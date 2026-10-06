{#
  Cross-adapter primitives. Prefer dbt-core's own portable macros (dbt.type_string(),
  dbt.string_literal(), api.Relation.create(), adapter.quote(), ...) everywhere they exist —
  they're used directly at each call site rather than wrapped here. This file holds only the
  handful of things dbt-core has no portable macro for, dispatched per adapter via
  adapter.dispatch(). Each has a default__ implementation that raises a clear compiler error
  naming the supported adapters, so an unsupported adapter fails at compile time with an
  actionable message instead of a confusing runtime SQL error.
#}

{% macro unsupported_adapter_error(feature) %}
  {% set supported_adapters = ['bigquery', 'postgres'] %}
  {{ exceptions.raise_compiler_error(
    "dbt-dqm's " ~ feature ~ " is not implemented for the '" ~ target.type ~ "' adapter. "
    ~ "Supported adapters: " ~ (supported_adapters | join(', ')) ~ "."
  ) }}
{% endmacro %}

{# A single-row source usable in a FROM clause to select typed literals from, e.g. building an
   empty typed CTE with `from {{ dbt_dqm.dual() }} where false`. BigQuery has no bare-SELECT
   dummy-row source usable with WHERE, so it needs UNNEST([1]); every other supported adapter can
   just select a literal row. #}
{% macro dual() %}
  {{ return(adapter.dispatch('dual', 'dbt_dqm')()) }}
{% endmacro %}

{% macro default__dual() %}
  (select 1 as dqm_dual) dqm_dual
{% endmacro %}

{% macro bigquery__dual() %}
  unnest([1])
{% endmacro %}

{# Lowercase hex-encoded SHA-256 of a string expression. Functional requirement, not a style
   choice (see docs/functional-requirements.md) — every supported adapter must implement this
   exact shape so unique_id/occurrence_id stay comparable across warehouses. Postgres requires
   the pgcrypto extension (`create extension if not exists pgcrypto`) for digest(); this isn't
   auto-created by the package since CREATE EXTENSION commonly needs elevated privileges the
   package's own role may not have. #}
{% macro sha256_hex(expression) %}
  {{ return(adapter.dispatch('sha256_hex', 'dbt_dqm')(expression)) }}
{% endmacro %}

{% macro default__sha256_hex(expression) %}
  {{ dbt_dqm.unsupported_adapter_error('SHA-256 hashing') }}
{% endmacro %}

{% macro bigquery__sha256_hex(expression) %}
  lower(to_hex(sha256({{ expression }})))
{% endmacro %}

{% macro postgres__sha256_hex(expression) %}
  lower(encode(digest({{ expression }}, 'sha256'), 'hex'))
{% endmacro %}

{# A JSON object serialized to a string, built from an ordered list of (key, value_sql) pairs.
   `key` is a plain Python/Jinja string (not itself SQL); `value_sql` is a SQL expression, already
   quoted/cast as needed by the caller. #}
{% macro json_object_string(pairs) %}
  {{ return(adapter.dispatch('json_object_string', 'dbt_dqm')(pairs)) }}
{% endmacro %}

{% macro default__json_object_string(pairs) %}
  {{ dbt_dqm.unsupported_adapter_error('JSON object construction') }}
{% endmacro %}

{% macro bigquery__json_object_string(pairs) %}
  to_json_string(struct(
    {% for key, value_sql in pairs %}
      {{ value_sql }} as {{ adapter.quote(key) }}{% if not loop.last %},{% endif %}
    {% endfor %}
  ))
{% endmacro %}

{% macro postgres__json_object_string(pairs) %}
  json_build_object(
    {% for key, value_sql in pairs %}
      {{ dbt_dqm.sql_string(key) }}, {{ value_sql }}{% if not loop.last %},{% endif %}
    {% endfor %}
  )::text
{% endmacro %}

{#
  Byte-stable issue identity input. Display JSON is intentionally adapter-native, but it must not
  be used as a hash preimage: warehouses are free to render semantically identical JSON with
  different whitespace. This length-prefixed representation is unambiguous and uses only character
  length plus already-normalized string values, whose behavior is identical on supported adapters.

  Shape: dqm-id-v1|k<key-length>:<key>|n| for null, or
         dqm-id-v1|k<key-length>:<key>|s<value-length>:<value>| for a string.
#}
{% macro canonical_identity_string(pairs) %}
  {{ return(adapter.dispatch('canonical_identity_string', 'dbt_dqm')(pairs)) }}
{% endmacro %}

{% macro default__canonical_identity_string(pairs) %}
  concat(
    {{ dbt_dqm.sql_string('dqm-id-v1|') }}
    {% for key, value_sql in pairs %}
      , {{ dbt_dqm.sql_string('k' ~ (key | length) ~ ':' ~ key ~ '|') }}
      , case
          when {{ value_sql }} is null then {{ dbt_dqm.sql_string('n|') }}
          else concat(
            {{ dbt_dqm.sql_string('s') }},
            cast(length({{ value_sql }}) as {{ dbt.type_string() }}),
            {{ dbt_dqm.sql_string(':') }},
            {{ value_sql }},
            {{ dbt_dqm.sql_string('|') }}
          )
        end
    {% endfor %}
  )
{% endmacro %}

{# Insert rows from source_sql whose key_columns don't already exist in target_relation; existing
   rows are left untouched (no update-on-match — every current caller only ever inserts
   immutable, invocation-scoped rows that are never revised in place). BigQuery has no unique/
   primary-key constraint concept, so it uses MERGE; Postgres uses INSERT ... ON CONFLICT, which
   requires a matching unique index — ensure_unique_index() below sets that up once per table. #}
{% macro insert_new_rows(target_relation, source_sql, key_columns, insert_columns) %}
  {{ return(adapter.dispatch('insert_new_rows', 'dbt_dqm')(target_relation, source_sql, key_columns, insert_columns)) }}
{% endmacro %}

{% macro default__insert_new_rows(target_relation, source_sql, key_columns, insert_columns) %}
  {{ dbt_dqm.unsupported_adapter_error('insert-if-not-matched') }}
{% endmacro %}

{% macro bigquery__insert_new_rows(target_relation, source_sql, key_columns, insert_columns) %}
  merge {{ target_relation }} t
  using ({{ source_sql }}) s
    on {% for column in key_columns %}t.{{ column }} = s.{{ column }}{% if not loop.last %} and {% endif %}{% endfor %}

  when not matched then insert (
    {{ insert_columns | join(', ') }}
  ) values (
    {% for column in insert_columns %}s.{{ column }}{% if not loop.last %}, {% endif %}{% endfor %}
  )
{% endmacro %}

{% macro postgres__insert_new_rows(target_relation, source_sql, key_columns, insert_columns) %}
  insert into {{ target_relation }} (
    {{ insert_columns | join(', ') }}
  )
  select {{ insert_columns | join(', ') }}
  from ({{ source_sql }}) s
  on conflict ({{ key_columns | join(', ') }}) do nothing
{% endmacro %}

{# Postgres-only: ON CONFLICT requires a unique index on exactly the conflict-target columns.
   No-op on adapters (like BigQuery) that don't need one. Safe to call every invocation. #}
{% macro ensure_unique_index(relation, columns) %}
  {{ return(adapter.dispatch('ensure_unique_index', 'dbt_dqm')(relation, columns)) }}
{% endmacro %}

{# Postgres query-support indexes. BigQuery uses clustering instead, so the default is a no-op. #}
{% macro ensure_index(relation, columns, suffix=none, where=none) %}
  {{ return(adapter.dispatch('ensure_index', 'dbt_dqm')(relation, columns, suffix, where)) }}
{% endmacro %}

{% macro default__ensure_index(relation, columns, suffix=none, where=none) %}
{% endmacro %}

{% macro postgres__ensure_index(relation, columns, suffix=none, where=none) %}
  {% if execute %}
    {% set index_suffix = suffix or (columns | join('_')) %}
    {% set index_name = (relation.identifier ~ '_' ~ index_suffix ~ '_idx') | lower %}
    {% set statement = 'create index if not exists ' ~ index_name ~ ' on ' ~ relation
      ~ ' (' ~ (columns | join(', ')) ~ ')' %}
    {% if where is not none %}{% set statement = statement ~ ' where ' ~ where %}{% endif %}
    {% do run_query(statement) %}
  {% endif %}
{% endmacro %}

{% macro default__ensure_unique_index(relation, columns) %}
{% endmacro %}

{% macro postgres__ensure_unique_index(relation, columns) %}
  {% if execute %}
    {% set index_name = (relation.identifier ~ '_' ~ (columns | join('_')) ~ '_key') | lower %}
    {% do run_query(
      'create unique index if not exists ' ~ index_name ~ ' on ' ~ relation
      ~ ' (' ~ (columns | join(', ')) ~ ')'
    ) %}
  {% endif %}
{% endmacro %}

{# The incremental strategy dqm_reconcile.sql and dqm_annotation_changes.sql should use per
   adapter. BigQuery supports 'merge' directly. dbt-postgres only gained native 'merge' support
   once targeting Postgres 15+; 'delete+insert' keyed by unique_key is supported on every
   Postgres version dbt-postgres supports and is semantically equivalent here, since every run
   regenerates the complete, correct set of rows for whatever it touches rather than partially
   patching existing rows. #}
{% macro incremental_upsert_strategy() %}
  {{ return('merge' if target.type == 'bigquery' else 'delete+insert') }}
{% endmacro %}

{% macro upsert_reconciliation_state(source_sql) %}
  {{ return(adapter.dispatch('upsert_reconciliation_state', 'dbt_dqm')(source_sql)) }}
{% endmacro %}

{% macro default__upsert_reconciliation_state(source_sql) %}
  {{ dbt_dqm.unsupported_adapter_error('reconciliation checkpoint upsert') }}
{% endmacro %}

{% macro bigquery__upsert_reconciliation_state(source_sql) %}
  merge {{ dbt_dqm.relation_name('dqm_reconciliation_state') }} t
  using ({{ source_sql }}) s
    on t.test_unique_id = s.test_unique_id
  when matched then update set captured_at = s.captured_at, invocation_id = s.invocation_id
  when not matched then insert (test_unique_id, captured_at, invocation_id)
    values (s.test_unique_id, s.captured_at, s.invocation_id)
{% endmacro %}

{% macro postgres__upsert_reconciliation_state(source_sql) %}
  insert into {{ dbt_dqm.relation_name('dqm_reconciliation_state') }} (
    test_unique_id, captured_at, invocation_id
  )
  {{ source_sql }}
  on conflict (test_unique_id) do update set
    captured_at = excluded.captured_at,
    invocation_id = excluded.invocation_id
{% endmacro %}
