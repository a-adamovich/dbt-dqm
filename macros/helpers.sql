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
    .replace("'", "\\'" )
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

{# Validate cost/retention variables at compile time. Accept YAML integers only: silently
   coercing strings, floats, zero, or negative values makes pruning behavior too easy to
   misunderstand. #}
{% macro positive_integer_var(name, default=none) %}
  {% set value = var(name, default) %}
  {% if value is none %}{{ return(none) }}{% endif %}
  {% if value is boolean or value is not number or value | int != value or value | int <= 0 %}
    {{ exceptions.raise_compiler_error(name ~ ' must be a positive integer when set.') }}
  {% endif %}
  {{ return(value | int) }}
{% endmacro %}

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
      granularity_signature {{ dbt.type_string() }},
      identity_scheme_signature {{ dbt.type_string() }}
      , source_unique_id {{ dbt.type_string() }}
      , source_relation {{ dbt.type_string() }}
      , test_severity {{ dbt.type_string() }}
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
    create table if not exists {{ dbt_dqm.relation_name('dqm_reconciliation_state') }} (
      test_unique_id {{ dbt.type_string() }} not null,
      captured_at {{ dbt.type_timestamp() }} not null,
      invocation_id {{ dbt.type_string() }} not null
    );
    create table if not exists {{ dbt_dqm.relation_name('dqm_schema_migrations') }} (
      migration_id {{ dbt.type_string() }} not null,
      applied_at {{ dbt.type_timestamp() }} not null
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
      granularity_signature {{ dbt.type_string() }},
      identity_scheme_signature {{ dbt.type_string() }}
      , source_unique_id {{ dbt.type_string() }}
      , source_relation {{ dbt.type_string() }}
      , test_severity {{ dbt.type_string() }}
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
    create table if not exists {{ dbt_dqm.relation_name('dqm_reconciliation_state') }} (
      test_unique_id {{ dbt.type_string() }} not null,
      captured_at {{ dbt.type_timestamp() }} not null,
      invocation_id {{ dbt.type_string() }} not null
    )
    cluster by test_unique_id;
    create table if not exists {{ dbt_dqm.relation_name('dqm_schema_migrations') }} (
      migration_id {{ dbt.type_string() }} not null,
      applied_at {{ dbt.type_timestamp() }} not null
    );
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
    {% set retention_days = dbt_dqm.positive_integer_var('dbt_dqm_retention_days', none) %}
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
    {% do dbt_dqm.ensure_column(execution_relation, 'identity_scheme_signature', dbt.type_string()) %}
    {% do dbt_dqm.ensure_column(execution_relation, 'source_unique_id', dbt.type_string()) %}
    {% do dbt_dqm.ensure_column(execution_relation, 'source_relation', dbt.type_string()) %}
    {% do dbt_dqm.ensure_column(execution_relation, 'test_severity', dbt.type_string()) %}
    {% do dbt_dqm.ensure_unique_index(execution_relation, ['invocation_id', 'test_unique_id']) %}
    {% do dbt_dqm.ensure_index(execution_relation, ['test_unique_id', 'captured_at', 'invocation_id'], 'reconcile') %}
    {% set observation_relation = api.Relation.create(
      database=target.database,
      schema=capture_schema,
      identifier='dqm_issue_observations',
      type='table'
    ) %}
    {% do dbt_dqm.ensure_unique_index(observation_relation, ['invocation_id', 'test_unique_id', 'unique_id']) %}
    {% do dbt_dqm.ensure_index(observation_relation, ['test_unique_id', 'unique_id', 'invocation_id'], 'identity_join') %}
    {% set state_relation = api.Relation.create(
      database=target.database,
      schema=capture_schema,
      identifier='dqm_reconciliation_state',
      type='table'
    ) %}
    {% do dbt_dqm.ensure_unique_index(state_relation, ['test_unique_id']) %}
    {% set migration_relation = api.Relation.create(
      database=target.database,
      schema=capture_schema,
      identifier='dqm_schema_migrations',
      type='table'
    ) %}
    {% do dbt_dqm.ensure_unique_index(migration_relation, ['migration_id']) %}
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
      {% do dbt_dqm.ensure_column(occurrence_relation, 'identity_scheme_signature', dbt.type_string()) %}
      {% do dbt_dqm.ensure_column(occurrence_relation, 'workflow_status', dbt.type_string()) %}
      {% do dbt_dqm.ensure_column(occurrence_relation, 'annotation_version', dbt.type_int()) %}
      {% do dbt_dqm.ensure_column(occurrence_relation, 'source_unique_id', dbt.type_string()) %}
      {% do dbt_dqm.ensure_column(occurrence_relation, 'source_relation', dbt.type_string()) %}
      {% do dbt_dqm.ensure_column(occurrence_relation, 'test_severity', dbt.type_string()) %}
      {% do run_query(
        'update ' ~ occurrence_relation
        ~ " set workflow_status = coalesce(workflow_status, test_status, 'NEW'), "
        ~ 'annotation_version = coalesce(annotation_version, 0)'
        ~ ' where workflow_status is null or annotation_version is null'
      ) %}
      {% do dbt_dqm.ensure_unique_index(occurrence_relation, ['occurrence_id']) %}
      {% do dbt_dqm.ensure_index(occurrence_relation, ['test_unique_id', 'unique_id', 'occurrence_number'], 'identity_history') %}
      {% do dbt_dqm.ensure_index(occurrence_relation, ['test_unique_id', 'unique_id'], 'active_identity', "record_status = 'Active'") %}
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

{# Record a package schema migration only after its idempotent migration body has succeeded. #}
{% macro record_schema_migration(migration_id) %}
  {% if execute %}
    {% set columns = ['migration_id', 'applied_at'] %}
    {% set source_sql %}
      select {{ dbt_dqm.sql_string(migration_id) }} as migration_id,
             {{ dbt.current_timestamp() }} as applied_at
    {% endset %}
    {% do run_query(dbt_dqm.insert_new_rows(
      dbt_dqm.relation_name('dqm_schema_migrations'), source_sql, ['migration_id'], columns
    )) %}
  {% endif %}
{% endmacro %}

{# Portable explicit cleanup for append-only collection logs. The reconciliation state and
   durable occurrence/audit tables are intentionally retained. #}
{% macro cleanup_dqm_logs(retention_days=none) %}
  {% if execute %}
    {% set days = retention_days %}
    {% if days is none %}{% set days = dbt_dqm.positive_integer_var('dbt_dqm_retention_days', none) %}{% endif %}
    {% if days is none %}
      {{ exceptions.raise_compiler_error(
        'cleanup_dqm_logs requires retention_days or the dbt_dqm_retention_days variable.'
      ) }}
    {% endif %}
    {% if days is boolean or days is not number or days | int != days or days | int <= 0 %}
      {{ exceptions.raise_compiler_error('retention_days must be a positive integer.') }}
    {% endif %}
    {% do run_query(
      'delete from ' ~ dbt_dqm.relation_name('dqm_issue_observations')
      ~ ' where observed_at < ' ~ dbt.dateadd('day', -1 * (days | int), dbt.current_timestamp())
    ) %}
    {% do run_query(
      'delete from ' ~ dbt_dqm.relation_name('dqm_test_executions')
      ~ ' where captured_at < ' ~ dbt.dateadd('day', -1 * (days | int), dbt.current_timestamp())
    ) %}
  {% endif %}
{% endmacro %}

{% macro ensure_annotation_indexes() %}
  {% if execute %}
    {% set relation = api.Relation.create(
      database=this.database,
      schema=this.schema,
      identifier='dqm_annotation_changes',
      type='table'
    ) %}
    {% do dbt_dqm.ensure_index(
      relation, ['occurrence_id', 'changed_at'], 'occurrence_audit'
    ) %}
  {% endif %}
{% endmacro %}

{# Advance the durable replay checkpoint only after dqm_reconcile has materialized successfully.
   If this hook fails, the same executions are replayed on the next run; deterministic occurrence
   IDs make that retry idempotent. #}
{% macro advance_reconciliation_state() %}
  {% if execute %}
    {% set tracked_test_ids = [] %}
    {% for node in graph.nodes.values() %}
      {% if node.resource_type == 'test' and dbt_dqm.tracked_test(node) %}
        {% do tracked_test_ids.append(node.unique_id) %}
      {% endif %}
    {% endfor %}
    {% if tracked_test_ids | length > 0 %}
      {% set quoted_ids = [] %}
      {% for test_id in tracked_test_ids %}{% do quoted_ids.append(dbt_dqm.sql_string(test_id)) %}{% endfor %}
      {% set source_sql %}
        select test_unique_id, captured_at, invocation_id
        from (
          select
            test_unique_id,
            captured_at,
            invocation_id,
            row_number() over (
              partition by test_unique_id order by captured_at desc, invocation_id desc
            ) as replay_rank
          from {{ dbt_dqm.relation_name('dqm_test_executions') }}
          where collection_status in ('success', 'not_applicable')
            and lower(test_status) in ('pass', 'warn', 'fail')
            and test_unique_id in ({{ quoted_ids | join(', ') }})
        ) ranked
        where replay_rank = 1
      {% endset %}
      {% do run_query(dbt_dqm.upsert_reconciliation_state(source_sql)) %}
    {% endif %}
  {% endif %}
{% endmacro %}
