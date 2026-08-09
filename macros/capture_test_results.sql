{#
  Persist dbt test execution metadata and row-level failure observations at on-run-end.

  Only selected tests tagged for dbt-dqm and configured with store_failures participate. Each
  failing test must declare meta.dbt_dqm.granularity, and every named key must be present in the
  stored-failure relation. Invalid configurations are recorded without making lifecycle changes.
#}
{% macro capture_test_results(results) %}
  {% if not execute or flags.WHICH not in ['test', 'build'] %}
    {{ return('') }}
  {% endif %}

  {% set tracked = [] %}
  {% for result in results %}
    {% if dbt_dqm.tracked_test(result.node) %}
      {% do tracked.append(result) %}
    {% endif %}
  {% endfor %}
  {% if tracked | length == 0 %}
    {{ return('') }}
  {% endif %}

  {% do dbt_dqm.ensure_capture_tables() %}

  {% set execution_columns = [
    'invocation_id', 'captured_at', 'command', 'test_unique_id', 'test_name', 'test_status',
    'failure_count', 'test_tags', 'test_meta', 'execution_time', 'message', 'failure_relation',
    'collection_status', 'collection_message', 'granularity_signature'
  ] %}
  {% set execution_rows = [] %}
  {% for result in tracked %}
    {% set node = result.node %}
    {% set tags_json = dbt_dqm.compact_json(node.config.tags) %}
    {% set meta_json = dbt_dqm.compact_json(node.config.meta or {}) %}
    {% set relation_text = node.relation_name if node.relation_name else none %}
    {# Configured granularity, order-preserved and case-folded. Recorded for every conclusive
       execution (not only ones with failures) so reconciliation can tell a genuine pass apart
       from a grain/identity-scheme change that happens to leave no rows failing under the old
       identity. #}
    {% set dqm_meta = (node.config.meta or {}).get('dbt_dqm', {}) %}
    {% set configured_grain = dqm_meta.get('granularity', []) or [] %}
    {% set granularity_signature = dbt_dqm.compact_json(configured_grain | map('lower') | list) %}
    {% set row %}
      select
        {{ dbt_dqm.sql_string(invocation_id) }} as invocation_id,
        {{ dbt.current_timestamp() }} as captured_at,
        {{ dbt_dqm.sql_string(flags.WHICH) }} as command,
        {{ dbt_dqm.sql_string(node.unique_id) }} as test_unique_id,
        {{ dbt_dqm.sql_string(node.name) }} as test_name,
        {{ dbt_dqm.sql_string(result.status) }} as test_status,
        cast({{ result.failures if result.failures is not none else 0 }} as {{ dbt.type_int() }}) as failure_count,
        {{ dbt_dqm.sql_string(tags_json) }} as test_tags,
        {{ dbt_dqm.sql_string(meta_json) }} as test_meta,
        cast({{ result.execution_time or 0 }} as {{ dbt.type_float() }}) as execution_time,
        {{ dbt_dqm.sql_string(result.message) }} as message,
        {{ dbt_dqm.sql_string(relation_text) }} as failure_relation,
        {{ dbt_dqm.sql_string('pending') }} as collection_status,
        cast(null as {{ dbt.type_string() }}) as collection_message,
        {{ dbt_dqm.sql_string(granularity_signature) }} as granularity_signature
    {% endset %}
    {% do execution_rows.append(row) %}
  {% endfor %}

  {% set execution_merge %}
    {{ dbt_dqm.insert_new_rows(
      dbt_dqm.relation_name('dqm_test_executions'),
      execution_rows | join('\nunion all\n'),
      ['invocation_id', 'test_unique_id'],
      execution_columns
    ) }}
  {% endset %}
  {% do run_query(execution_merge) %}

  {% set observation_columns = [
    'invocation_id', 'observed_at', 'test_unique_id', 'test_name', 'unique_id',
    'record_values_json', 'failure_row_count', 'initial_poc_responsible', 'initial_call_to_action',
    'test_tags'
  ] %}

  {# Tests with failing rows commit their own observation merge and status update immediately,
     one test at a time, rather than deferring both to one batched pair of statements at the very
     end. Jinja has no try/except, so a single error anywhere in this loop still aborts the
     macro — but with per-test commits, everything already processed before that point is
     durably saved instead of lost, and only the test being processed (and any after it) are left
     'pending' for a future invocation to pick up. Zero-failure tests carry no observation payload
     to protect, so they're still flipped to 'not_applicable' in one cheap batched update below. #}
  {% set successful_test_ids = [] %}
  {% for result in tracked %}
    {% set node = result.node %}
    {% if result.failures is not none and result.failures | int > 0 and node.relation_name %}
      {# Construct directly: dbt's relation cache can be stale immediately after store_failures. #}
      {% set relation = api.Relation.create(
        database=node.database,
        schema=node.schema,
        identifier=node.alias,
        type='table'
      ) %}
      {% set columns = adapter.get_columns_in_relation(relation) %}
      {% if columns | length > 0 %}
        {% set meta = node.config.meta or {} %}
        {% set dqm_meta = meta.get('dbt_dqm', {}) %}
        {% set configured_grain = dqm_meta.get('granularity', []) or [] %}
        {% set grain_lower = configured_grain | map('lower') | list %}
        {% set key_columns = [] %}
        {# Configured order is part of the canonical identity and must remain stable. #}
        {% for grain_name in grain_lower %}
          {% for column in columns %}
            {% if column.name | lower == grain_name %}{% do key_columns.append(column) %}{% endif %}
          {% endfor %}
        {% endfor %}
        {% if grain_lower | length == 0 %}
          {% set error_update %}
            update {{ dbt_dqm.relation_name('dqm_test_executions') }}
            set collection_status = {{ dbt_dqm.sql_string('configuration_error') }},
                collection_message = {{ dbt_dqm.sql_string('meta.dbt_dqm.granularity is required and cannot be empty.') }}
            where invocation_id = {{ dbt_dqm.sql_string(invocation_id) }}
              and test_unique_id = {{ dbt_dqm.sql_string(node.unique_id) }}
          {% endset %}
          {% do run_query(error_update) %}
          {% do log('dbt-dqm skipped ' ~ node.unique_id ~ ': granularity is required', info=true) %}
        {% elif key_columns | length != grain_lower | length %}
          {% set error_update %}
            update {{ dbt_dqm.relation_name('dqm_test_executions') }}
            set collection_status = {{ dbt_dqm.sql_string('configuration_error') }},
                collection_message = {{ dbt_dqm.sql_string(
                  'Every meta.dbt_dqm.granularity column must exist in the test output. Configured: '
                  ~ (configured_grain | join(', '))
                ) }}
            where invocation_id = {{ dbt_dqm.sql_string(invocation_id) }}
              and test_unique_id = {{ dbt_dqm.sql_string(node.unique_id) }}
          {% endset %}
          {% do run_query(error_update) %}
          {% do log('dbt-dqm skipped ' ~ node.unique_id ~ ': a granularity column is missing', info=true) %}
        {% else %}
          {% set tags_json = dbt_dqm.compact_json(node.config.tags) %}
          {% set owner = dqm_meta.get('poc_responsible') %}
          {% set action = dqm_meta.get('call_to_action') %}

          {% set key_pairs = [] %}
          {% for column in key_columns %}
            {% set value_sql %}
              case when {{ adapter.quote(column.name) }} is null then null
                   else lower(trim(cast({{ adapter.quote(column.name) }} as {{ dbt.type_string() }}))) end
            {% endset %}
            {% do key_pairs.append((column.name | lower, value_sql)) %}
          {% endfor %}

          {% set record_pairs = [] %}
          {% for column in columns %}
            {% set value_sql %}
              case when {{ adapter.quote(column.name) }} is null then null
                   else cast({{ adapter.quote(column.name) }} as {{ dbt.type_string() }}) end
            {% endset %}
            {% do record_pairs.append((column.name, value_sql)) %}
          {% endfor %}

          {% set query %}
            with failure_rows as (
              select
                {{ dbt_dqm.sha256_hex(dbt_dqm.json_object_string(key_pairs)) }} as unique_id,
                {{ dbt_dqm.json_object_string(record_pairs) }} as record_values_json
              from {{ relation }}
            ),
            deduplicated_failures as (
              select
              {{ dbt_dqm.sql_string(invocation_id) }} as invocation_id,
              {{ dbt.current_timestamp() }} as observed_at,
              {{ dbt_dqm.sql_string(node.unique_id) }} as test_unique_id,
              {{ dbt_dqm.sql_string(node.name) }} as test_name,
              unique_id,
              record_values_json,
              count(*) over (partition by unique_id) as failure_row_count,
              row_number() over (
                partition by unique_id
                order by record_values_json
              ) as representative_rank
              from failure_rows
            )
            select
              invocation_id,
              observed_at,
              test_unique_id,
              test_name,
              unique_id,
              record_values_json,
              failure_row_count,
              {{ dbt_dqm.sql_string(owner) }} as initial_poc_responsible,
              {{ dbt_dqm.sql_string(action) }} as initial_call_to_action,
              {{ dbt_dqm.sql_string(tags_json) }} as test_tags
            from deduplicated_failures
            where representative_rank = 1
          {% endset %}
          {% set observation_merge %}
            {{ dbt_dqm.insert_new_rows(
              dbt_dqm.relation_name('dqm_issue_observations'),
              query,
              ['invocation_id', 'test_unique_id', 'unique_id'],
              observation_columns
            ) }}
          {% endset %}
          {% do run_query(observation_merge) %}
          {% set status_update %}
            update {{ dbt_dqm.relation_name('dqm_test_executions') }}
            set collection_status = {{ dbt_dqm.sql_string('success') }}
            where invocation_id = {{ dbt_dqm.sql_string(invocation_id) }}
              and test_unique_id = {{ dbt_dqm.sql_string(node.unique_id) }}
              and collection_status = {{ dbt_dqm.sql_string('pending') }}
          {% endset %}
          {% do run_query(status_update) %}
        {% endif %}
      {% else %}
        {% set relation_error %}
          update {{ dbt_dqm.relation_name('dqm_test_executions') }}
          set collection_status = {{ dbt_dqm.sql_string('collection_error') }},
              collection_message = {{ dbt_dqm.sql_string('The stored-failure relation was not available.') }}
          where invocation_id = {{ dbt_dqm.sql_string(invocation_id) }}
            and test_unique_id = {{ dbt_dqm.sql_string(node.unique_id) }}
        {% endset %}
        {% do run_query(relation_error) %}
        {% do log('dbt-dqm could not read stored failures for ' ~ node.unique_id, info=true) %}
      {% endif %}
    {% else %}
      {% do successful_test_ids.append(node.unique_id) %}
    {% endif %}
  {% endfor %}

  {# Only zero-failure tests remain here — tests with failure evidence already committed their
     own observation merge and status update above. There's no observation payload at risk for a
     zero-failure test, so one batched update for all of them is safe and keeps this cheap. #}
  {% if successful_test_ids | length > 0 %}
    {% set quoted_ids = [] %}
    {% for test_id in successful_test_ids %}{% do quoted_ids.append(dbt_dqm.sql_string(test_id)) %}{% endfor %}
    {% set success_update %}
      update {{ dbt_dqm.relation_name('dqm_test_executions') }}
      set collection_status = {{ dbt_dqm.sql_string('not_applicable') }}
      where invocation_id = {{ dbt_dqm.sql_string(invocation_id) }}
        and test_unique_id in ({{ quoted_ids | join(',') }})
        and collection_status = {{ dbt_dqm.sql_string('pending') }}
    {% endset %}
    {% do run_query(success_update) %}
  {% endif %}
{% endmacro %}
