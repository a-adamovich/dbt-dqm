{# Render a Python/Jinja value as a SQL string literal, dispatched per adapter because string-
   literal escaping rules genuinely differ (BigQuery interprets backslash escapes in a plain
   quoted literal; Postgres's standard-conforming strings treat backslash as a literal
   character, so only the quote character itself needs doubling). #}
{% macro sql_string(value) %}
  {{ return(adapter.dispatch('sql_string', 'dbt_dqm')(value)) }}
{% endmacro %}

{% macro default__sql_string(value) %}
  {{ dbt_dqm.unsupported_adapter_error('string literal escaping') }}
{% endmacro %}

{% macro bigquery__sql_string(value) -%}
  {%- if value is none -%}cast(null as {{ dbt.type_string() }})
  {%- else -%}'{{ (value | string)
    .replace('\\', '\\\\')
    .replace("'", "''")
    .replace('\r', '\\r')
    .replace('\n', '\\n') }}'
  {%- endif -%}
{%- endmacro %}

{% macro postgres__sql_string(value) -%}
  {%- if value is none -%}cast(null as {{ dbt.type_string() }})
  {%- else -%}'{{ (value | string).replace("'", "''") }}'
  {%- endif -%}
{%- endmacro %}

{# Decide whether a result node is an explicitly tagged, stored-failure dbt data test. #}
{% macro tracked_test(node) -%}
  {%- set tracked_tag = var('dbt_dqm_test_tag', 'dqm') -%}
  {{ return(
    node.resource_type == 'test'
    and node.config.store_failures == true
    and tracked_tag in node.config.tags
  ) }}
{%- endmacro %}

{# Resolve a dbt-dqm warehouse relation using the target and optional schema override.
   api.Relation.create() renders correctly per adapter (BigQuery backticks, Postgres double
   quotes, ...) instead of hand-rolling one quoting convention. #}
{% macro relation_name(name) -%}
  {%- set custom_schema = var('dbt_dqm_schema', none) -%}
  {%- set schema = target.schema if custom_schema is none else generate_schema_name(custom_schema, none) -%}
  {{ api.Relation.create(database=target.database, schema=schema, identifier=name) }}
{%- endmacro %}

{# Serialize manifest values to stable compact JSON for warehouse storage. #}
{% macro compact_json(value) -%}
  {{ return(tojson(value).replace(', ', ',').replace(': ', ':')) }}
{%- endmacro %}

{# Create the hook-owned execution and observation tables when they do not yet exist. On
   BigQuery, partitioned by their event date and clustered by test so reconciliation and any
   future time-scoped query can prune instead of scanning the whole append-only history;
   partitioning/clustering are BigQuery-specific concepts with no portable equivalent, so this is
   the one piece of DDL kept fully adapter-dispatched rather than sharing one body. #}
{% macro capture_tables_ddl() %}
  {{ return(adapter.dispatch('capture_tables_ddl', 'dbt_dqm')()) }}
{% endmacro %}

{% macro default__capture_tables_ddl() %}
    create table if not exists {{ dbt_dqm.relation_name('dqm_test_executions') }} (
      invocation_id {{ dbt.type_string() }} not null,
      captured_at {{ dbt.type_timestamp() }} not null,
      command {{ dbt.type_string() }},
      test_unique_id {{ dbt.type_string() }} not null,
      test_name {{ dbt.type_string() }},
      test_status {{ dbt.type_string() }},
      failure_count {{ dbt.type_int() }},
      test_tags {{ dbt.type_string() }},
      test_meta {{ dbt.type_string() }},
      execution_time {{ dbt.type_float() }},
      message {{ dbt.type_string() }},
      failure_relation {{ dbt.type_string() }},
      collection_status {{ dbt.type_string() }},
      collection_message {{ dbt.type_string() }},
      granularity_signature {{ dbt.type_string() }}
    );
    create table if not exists {{ dbt_dqm.relation_name('dqm_issue_observations') }} (
      invocation_id {{ dbt.type_string() }} not null,
      observed_at {{ dbt.type_timestamp() }} not null,
      test_unique_id {{ dbt.type_string() }} not null,
      test_name {{ dbt.type_string() }},
      unique_id {{ dbt.type_string() }} not null,
      record_values_json {{ dbt.type_string() }},
      failure_row_count {{ dbt.type_int() }},
      initial_poc_responsible {{ dbt.type_string() }},
      initial_call_to_action {{ dbt.type_string() }},
      test_tags {{ dbt.type_string() }}
    );
{% endmacro %}

{% macro bigquery__capture_tables_ddl() %}
    create table if not exists {{ dbt_dqm.relation_name('dqm_test_executions') }} (
      invocation_id {{ dbt.type_string() }} not null,
      captured_at {{ dbt.type_timestamp() }} not null,
      command {{ dbt.type_string() }},
      test_unique_id {{ dbt.type_string() }} not null,
      test_name {{ dbt.type_string() }},
      test_status {{ dbt.type_string() }},
      failure_count {{ dbt.type_int() }},
      test_tags {{ dbt.type_string() }},
      test_meta {{ dbt.type_string() }},
      execution_time {{ dbt.type_float() }},
      message {{ dbt.type_string() }},
      failure_relation {{ dbt.type_string() }},
      collection_status {{ dbt.type_string() }},
      collection_message {{ dbt.type_string() }},
      granularity_signature {{ dbt.type_string() }}
    )
    partition by date(captured_at)
    cluster by test_unique_id;
    create table if not exists {{ dbt_dqm.relation_name('dqm_issue_observations') }} (
      invocation_id {{ dbt.type_string() }} not null,
      observed_at {{ dbt.type_timestamp() }} not null,
      test_unique_id {{ dbt.type_string() }} not null,
      test_name {{ dbt.type_string() }},
      unique_id {{ dbt.type_string() }} not null,
      record_values_json {{ dbt.type_string() }},
      failure_row_count {{ dbt.type_int() }},
      initial_poc_responsible {{ dbt.type_string() }},
      initial_call_to_action {{ dbt.type_string() }},
      test_tags {{ dbt.type_string() }}
    )
    partition by date(observed_at)
    cluster by test_unique_id, unique_id;
{% endmacro %}

{# Apply the configured retention window, if any, to the hook-owned log tables via BigQuery
   partition expiration — a BigQuery-specific mechanism with no portable equivalent, so this is a
   no-op on every other adapter. Unset (the default) means these tables are never pruned
   automatically — this is opt-in because expiring a test's only recorded execution stops it from
   being reconciled until it runs again; it never corrupts or reopens an already-reconciled
   occurrence, since dqm_issue_occurrences is a separate, persisted table decoupled from this raw
   history. Set `dbt_dqm_retention_days` once you've confirmed that window comfortably exceeds how
   infrequently your slowest tracked test runs. #}
{% macro apply_retention_policy() %}
  {{ return(adapter.dispatch('apply_retention_policy', 'dbt_dqm')()) }}
{% endmacro %}

{% macro default__apply_retention_policy() %}
{% endmacro %}

{% macro bigquery__apply_retention_policy() %}
  {% if execute %}
    {% set retention_days = var('dbt_dqm_retention_days', none) %}
    {% if retention_days is not none %}
      {% set custom_schema = var('dbt_dqm_schema', none) %}
      {% set capture_schema = target.schema if custom_schema is none else generate_schema_name(custom_schema, none) %}
      {% for table_name in ['dqm_test_executions', 'dqm_issue_observations'] %}
        {% set relation = api.Relation.create(
          database=target.database,
          schema=capture_schema,
          identifier=table_name,
          type='table'
        ) %}
        {% do run_query(
          'alter table ' ~ relation ~ ' set options(partition_expiration_days = '
          ~ (retention_days | int) ~ ')'
        ) %}
      {% endfor %}
    {% endif %}
  {% endif %}
{% endmacro %}

{# Add a column to a relation if it is not already present. Generic replacement for hand-written,
   one-off migration branches: reuse this for every future additive schema change instead of adding
   another hardcoded column-specific block. `add column` syntax is shared across every supported
   adapter, so this needs no dispatch — only column_type (pass a portable dbt.type_*() value). #}
{% macro ensure_column(relation, column_name, column_type) %}
  {% if execute %}
    {% set column_names = adapter.get_columns_in_relation(relation) | map(attribute='name') | map('lower') | list %}
    {% if column_name | lower not in column_names %}
      {% do run_query('alter table ' ~ relation ~ ' add column ' ~ column_name ~ ' ' ~ column_type) %}
    {% endif %}
  {% endif %}
{% endmacro %}

{# Initialize capture tables and migrate only columns that are actually present or absent. #}
{% macro ensure_capture_tables() %}
  {% if execute %}
    {% do run_query(dbt_dqm.capture_tables_ddl()) %}
    {% do dbt_dqm.apply_retention_policy() %}
    {% set custom_schema = var('dbt_dqm_schema', none) %}
    {% set capture_schema = target.schema if custom_schema is none else generate_schema_name(custom_schema, none) %}
    {% set execution_relation = api.Relation.create(
      database=target.database,
      schema=capture_schema,
      identifier='dqm_test_executions',
      type='table'
    ) %}
    {% do dbt_dqm.ensure_column(execution_relation, 'granularity_signature', dbt.type_string()) %}
    {% do dbt_dqm.ensure_unique_index(execution_relation, ['invocation_id', 'test_unique_id']) %}
    {% set observation_relation = api.Relation.create(
      database=target.database,
      schema=capture_schema,
      identifier='dqm_issue_observations',
      type='table'
    ) %}
    {% do dbt_dqm.ensure_unique_index(observation_relation, ['invocation_id', 'test_unique_id', 'unique_id']) %}
    {% set column_names = adapter.get_columns_in_relation(observation_relation) | map(attribute='name') | map('lower') | list %}
    {% if 'record_values_json' not in column_names %}
      {% do run_query('alter table ' ~ observation_relation ~ ' add column record_values_json ' ~ dbt.type_string()) %}
      {% do column_names.append('record_values_json') %}
    {% endif %}
    {% if 'key_values_json' in column_names or 'attribute_values_json' in column_names %}
      {% if 'key_values_json' in column_names and 'attribute_values_json' in column_names %}
        {% set legacy_payload %}
          case
            when key_values_json is null then attribute_values_json
            when attribute_values_json is null then key_values_json
            else concat(substr(key_values_json, 1, length(key_values_json) - 1), ',', substr(attribute_values_json, 2))
          end
        {% endset %}
      {% elif 'key_values_json' in column_names %}
        {% set legacy_payload %}key_values_json{% endset %}
      {% else %}
        {% set legacy_payload %}attribute_values_json{% endset %}
      {% endif %}
      {% set backfill_sql %}
        update {{ observation_relation }}
        set record_values_json = {{ legacy_payload }}
        where record_values_json is null
      {% endset %}
      {% do run_query(backfill_sql) %}
    {% endif %}
    {% set drop_columns = [] %}
    {% for legacy_column in ['key_values_json', 'attribute_values_json', 'key_col_names', 'key_col_values', 'attr_col_names', 'attr_col_values'] %}
      {% if legacy_column in column_names %}{% do drop_columns.append(legacy_column) %}{% endif %}
    {% endfor %}
    {% if drop_columns | length > 0 %}
      {% do run_query('alter table ' ~ observation_relation ~ ' drop column ' ~ (drop_columns | join(', drop column '))) %}
    {% endif %}
  {% endif %}
{% endmacro %}

{# Migrate an existing occurrence table only when its actual schema requires a change. #}
{% macro migrate_occurrence_schema() %}
  {% if execute %}
    {% set occurrence_relation = adapter.get_relation(
      database=target.database,
      schema=this.schema,
      identifier='dqm_issue_occurrences'
    ) %}
    {% if occurrence_relation is not none %}
      {% do dbt_dqm.ensure_column(occurrence_relation, 'granularity_signature', dbt.type_string()) %}
      {% set column_names = adapter.get_columns_in_relation(occurrence_relation) | map(attribute='name') | map('lower') | list %}
      {% if 'record_values_json' not in column_names %}
        {% do run_query('alter table ' ~ occurrence_relation ~ ' add column record_values_json ' ~ dbt.type_string()) %}
        {% do column_names.append('record_values_json') %}
      {% endif %}
      {% if 'key_values_json' in column_names or 'attribute_values_json' in column_names %}
        {% if 'key_values_json' in column_names and 'attribute_values_json' in column_names %}
          {% set legacy_payload %}
            case
              when key_values_json is null then attribute_values_json
              when attribute_values_json is null then key_values_json
              else concat(substr(key_values_json, 1, length(key_values_json) - 1), ',', substr(attribute_values_json, 2))
            end
          {% endset %}
        {% elif 'key_values_json' in column_names %}
          {% set legacy_payload %}key_values_json{% endset %}
        {% else %}
          {% set legacy_payload %}attribute_values_json{% endset %}
        {% endif %}
        {% set backfill_sql %}
          update {{ occurrence_relation }}
          set record_values_json = {{ legacy_payload }}
          where record_values_json is null
        {% endset %}
        {% do run_query(backfill_sql) %}
      {% endif %}
      {% set drop_columns = [] %}
      {% for legacy_column in ['key_values_json', 'attribute_values_json', 'key_col_names', 'key_col_values', 'attr_col_names', 'attr_col_values'] %}
        {% if legacy_column in column_names %}{% do drop_columns.append(legacy_column) %}{% endif %}
      {% endfor %}
      {% if drop_columns | length > 0 %}
        {% do run_query('alter table ' ~ occurrence_relation ~ ' drop column ' ~ (drop_columns | join(', drop column '))) %}
      {% endif %}
    {% endif %}
  {% endif %}
{% endmacro %}
