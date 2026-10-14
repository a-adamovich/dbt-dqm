{% macro column_value_sql(column) %}
  {% set value = adapter.quote(column.name) %}
  case when {{ value }} is null then null else
  {% if target.type == 'bigquery' and (column.dtype|upper in ['RECORD','STRUCT','JSON'] or column.mode|default('') == 'REPEATED' or column.data_type|upper|truncate(5, true, '') == 'ARRAY') %}
    to_json_string({{ value }})
  {% elif target.type == 'bigquery' and column.dtype|upper == 'BYTES' %}
    to_base64({{ value }})
  {% elif target.type == 'bigquery' and column.dtype|upper == 'GEOGRAPHY' %}
    st_astext({{ value }})
  {% else %}cast({{ value }} as {{ dbt.type_string() }}){% endif %} end
{% endmacro %}

{# Scope the ordering wrapper to enabled DQM tests; ordinary tests keep dbt's SQL. #}
{% macro get_limit_subquery_sql(sql, limit) %}
  {% if not var('dqm_enabled', false) or model.resource_type != 'test' or 'dbt_dqm' not in (model.config.meta or {}) %}
    {{ return(dbt.get_limit_subquery_sql(sql, limit)) }}
  {% endif %}
  {% set budget = limit if limit is not none else dbt_dqm.positive_integer_var('dbt_dqm_default_test_limit',100) %}
  {% set grain = model.config.meta.dbt_dqm.get('granularity', []) %}
  {% set names = var('_dbt_dqm_output_columns',{}).get(model.unique_id) or dbt.get_columns_in_query(sql) %}
  {% set keys = [] %}
  {% for key in grain %}
    {% set found = [] %}
    {% for name in names %}{% if name|lower == key|trim|lower %}{% do found.append(name) %}{% endif %}{% endfor %}
    {% if found|length != 1 %}{{ exceptions.raise_compiler_error('DQM granularity column missing or ambiguous: ' ~ key ~ ' in ' ~ model.original_file_path) }}{% endif %}
    {% do keys.append((key|trim|lower, 'lower(trim(cast(dqm_rows.' ~ adapter.quote(found[0]) ~ ' as ' ~ dbt.type_string() ~ ')))')) %}
  {% endfor %}
  select * from ({{ sql }}) dqm_rows
  order by {{ dbt_dqm.sha256_hex(dbt_dqm.canonical_identity_string(keys)) }},
    {% if target.type=='bigquery' %}to_json_string(dqm_rows){% else %}to_jsonb(dqm_rows)::text{% endif %}
  limit {{ budget }}
{% endmacro %}

{% macro initialize_dqm() %}
  {% if not execute or not var('dqm_enabled',false) %}{{ return('') }}{% endif %}
  {% set anchor=dbt_dqm.dqm_relation('dqm_install') %}
  {% if not adapter.check_schema_exists(anchor.database, anchor.schema) %}
    {% do adapter.create_schema(anchor) %}
  {% endif %}
  {% set script %}{% if target.type=='postgres' %}begin; {{ dbt_dqm.reconcile_lock() }}{% endif %}
    {{ dbt_dqm.setup_sql() }}{% if target.type=='postgres' %}commit;{% endif %}{% endset %}
  {% do run_query(script) %}
{% endmacro %}

{% macro collect_results(payload) %}
  {% if not execute or not var('dqm_enabled',false) %}{{ return('') }}{% endif %}
  {% set results=[] %}
  {% for row in payload.results %}
    {% set node=graph.nodes.get(row.unique_id) %}
    {% if node is none %}{{ exceptions.raise_compiler_error('DQM test manifest changed before capture: ' ~ row.unique_id) }}{% endif %}
    {% do results.append({'node':node,'status':row.status,'failures':row.failures,'execution_time':row.execution_time,'message':row.message}) %}
  {% endfor %}
  {% set summary_sql %}
    select test_name,collection_status,collection_message,captured_issue_count
    from {{ dbt_dqm.dqm_relation('dqm_test_executions') }}
    where invocation_id={{ dbt_dqm.sql_string(payload.invocation_id) }}
  {% endset %}
  {% set failures=run_query(dbt_dqm.capture_test_results(results,payload.invocation_id,initialized=true) ~ summary_sql) %}
  {% set errors=[] %}
  {% set counts=namespace(captured=0) %}
  {% for row in failures.rows %}
    {% set counts.captured=counts.captured+(row[3] or 0) %}
    {% if row[1] not in ['success','not_applicable','limit_reached'] %}
      {% do errors.append({'test_name':row[0],'status':row[1],'message':row[2]}) %}
    {% endif %}
  {% endfor %}
  {% do log('DQM_COLLECTION_ERRORS=' ~ tojson(errors),info=false) %}
  {% do log('DQM_COLLECTION_SUMMARY=' ~ tojson({'captured_issue_count':counts.captured}),info=false) %}
{% endmacro %}

{% macro maintain_dqm() %}
  {% if execute and var('dqm_enabled',false) %}
    {% set observed=dbt_dqm.maintenance_run(dbt_dqm.maintenance_retention_days(), dbt_dqm.positive_integer_var('dbt_dqm_event_retention_days',none), 'automatic') %}
    {% do log('DQM_MAINTENANCE=' ~ tojson(observed),info=false) %}
  {% endif %}
{% endmacro %}

{% macro workflow_settings() %}
  {% do log('DQM_SETTINGS=' ~ tojson({'default_limit': dbt_dqm.positive_integer_var('dbt_dqm_default_test_limit',100), 'raw_retention': dbt_dqm.maintenance_retention_days(), 'event_retention':dbt_dqm.positive_integer_var('dbt_dqm_event_retention_days',none), 'event_payloads':dbt_dqm.event_payload_mode(), 'legacy_tag':var('dbt_dqm_test_tag',none), 'legacy_capture':var('dbt_dqm_capture_mode',none), 'refresh':dbt_dqm.refresh_contract()}),info=false) %}
{% endmacro %}
