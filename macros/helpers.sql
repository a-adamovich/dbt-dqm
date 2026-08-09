{# Render a Python/Jinja value as a BigQuery STRING literal with control characters escaped. #}
{% macro sql_string(value) -%}
  {%- if value is none -%}cast(null as string)
  {%- else -%}'{{ (value | string)
    .replace('\\', '\\\\')
    .replace("'", "''")
    .replace('\r', '\\r')
    .replace('\n', '\\n') }}'
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

{# Resolve a dbt-dqm warehouse relation using the target and optional schema override. #}
{% macro relation_name(name) -%}
  {%- set custom_schema = var('dbt_dqm_schema', none) -%}
  {%- set schema = target.schema if custom_schema is none else generate_schema_name(custom_schema, none) -%}
  `{{ target.database }}.{{ schema }}.{{ name }}`
{%- endmacro %}

{# Serialize manifest values to stable compact JSON for warehouse storage. #}
{% macro compact_json(value) -%}
  {{ return(tojson(value).replace(', ', ',').replace(': ', ':')) }}
{%- endmacro %}

{# Create the hook-owned execution and observation tables when they do not yet exist. #}
{% macro capture_tables_ddl() %}
    create table if not exists {{ dbt_dqm.relation_name('dqm_test_executions') }} (
      invocation_id string not null,
      captured_at timestamp not null,
      command string,
      test_unique_id string not null,
      test_name string,
      test_status string,
      failure_count int64,
      test_tags string,
      test_meta string,
      execution_time float64,
      message string,
      failure_relation string,
      collection_status string,
      collection_message string,
      granularity_signature string
    );
    create table if not exists {{ dbt_dqm.relation_name('dqm_issue_observations') }} (
      invocation_id string not null,
      observed_at timestamp not null,
      test_unique_id string not null,
      test_name string,
      unique_id string not null,
      record_values_json string,
      failure_row_count int64,
      initial_poc_responsible string,
      initial_call_to_action string,
      test_tags string
    );
{% endmacro %}

{# Add a column to a relation if it is not already present. Generic replacement for hand-written,
   one-off migration branches: reuse this for every future additive schema change instead of adding
   another hardcoded column-specific block. #}
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
    {% set custom_schema = var('dbt_dqm_schema', none) %}
    {% set capture_schema = target.schema if custom_schema is none else generate_schema_name(custom_schema, none) %}
    {% set execution_relation = api.Relation.create(
      database=target.database,
      schema=capture_schema,
      identifier='dqm_test_executions',
      type='table'
    ) %}
    {% do dbt_dqm.ensure_column(execution_relation, 'granularity_signature', 'string') %}
    {% set observation_relation = api.Relation.create(
      database=target.database,
      schema=capture_schema,
      identifier='dqm_issue_observations',
      type='table'
    ) %}
    {% set column_names = adapter.get_columns_in_relation(observation_relation) | map(attribute='name') | map('lower') | list %}
    {% if 'record_values_json' not in column_names %}
      {% do run_query('alter table ' ~ observation_relation ~ ' add column record_values_json string') %}
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
      {% do dbt_dqm.ensure_column(occurrence_relation, 'granularity_signature', 'string') %}
      {% set column_names = adapter.get_columns_in_relation(occurrence_relation) | map(attribute='name') | map('lower') | list %}
      {% if 'record_values_json' not in column_names %}
        {% do run_query('alter table ' ~ occurrence_relation ~ ' add column record_values_json string') %}
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
