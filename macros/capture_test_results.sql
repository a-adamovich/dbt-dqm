{#
  Persist dbt test execution metadata and row-level failure observations at on-run-end.

  Only selected tests tagged for dbt-dqm and configured with store_failures participate. Each
  failing test must declare meta.dbt_dqm.granularity, and every named key must be present in the
  stored-failure relation. Invalid configurations are recorded without making lifecycle changes.
#}
{% macro capture_test_results(results) %}
  {% if not execute or dbt_dqm.empty_mode() or flags.WHICH not in ['test', 'build'] %}
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

  {% set capture_sql = [] %}
  {% set setup %}{% if target.type=='postgres' %}begin; {{ dbt_dqm.reconcile_lock() }}{% endif %}
    {{ dbt_dqm.setup_sql() }}{% if target.type=='postgres' %}commit;{% endif %}{% endset %}
  {% do capture_sql.append(setup) %}

  {% set execution_columns = [
    'invocation_id', 'captured_at', 'command', 'test_unique_id', 'test_name', 'test_status',
    'failure_count', 'test_tags', 'test_meta', 'execution_time', 'message', 'failure_relation',
    'collection_status', 'collection_message', 'granularity_signature',
    'identity_scheme_signature', 'source_unique_id', 'source_relation', 'test_severity',
    'test_priority', 'test_criticality', 'owner_conflict_identity_count', 'capture_mode'
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
    {% set signature_grain = [] %}
    {% if configured_grain is sequence and configured_grain is not string %}
      {% for grain_name in configured_grain %}
        {% if grain_name is string %}{% do signature_grain.append(grain_name | trim | lower) %}{% endif %}
      {% endfor %}
    {% endif %}
    {% set granularity_signature = dbt_dqm.compact_json(signature_grain) %}
    {% set identity_scheme_signature = dbt_dqm.compact_json({
      'version': 'dqm-id-v1',
      'granularity': signature_grain
    }) %}
    {% set source = namespace(unique_id=none, relation=none) %}
    {% for dependency_id in node.depends_on.nodes %}
      {% if source.unique_id is none %}
        {% set dependency = graph.nodes.get(dependency_id, graph.sources.get(dependency_id)) %}
        {% if dependency is not none and dependency.resource_type in ['model', 'seed', 'snapshot', 'source'] %}
          {% set source.unique_id = dependency.unique_id %}
          {% set source.relation = dependency.relation_name %}
        {% endif %}
      {% endif %}
    {% endfor %}
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
        {{ dbt_dqm.sql_string(granularity_signature) }} as granularity_signature,
        {{ dbt_dqm.sql_string(identity_scheme_signature) }} as identity_scheme_signature,
        {{ dbt_dqm.sql_string(source.unique_id) }} as source_unique_id,
        {{ dbt_dqm.sql_string(source.relation) }} as source_relation,
        {{ dbt_dqm.sql_string(node.config.severity) }} as test_severity,
        {{ dbt_dqm.sql_string(dqm_meta.get('priority')|trim|lower if dqm_meta.get('priority') is string and dqm_meta.get('priority')|trim|lower != 'unset' else none) }} as test_priority,
        {{ dbt_dqm.sql_string(dqm_meta.get('criticality')|trim if dqm_meta.get('criticality') is string else none) }} as test_criticality,
        0 as owner_conflict_identity_count,
        {{ dbt_dqm.sql_string(dqm_meta.get('capture_mode',var('dbt_dqm_capture_mode','identity_only'))) }} as capture_mode
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
  {% do capture_sql.append(execution_merge ~ ';') %}

  {% set observation_columns = [
    'invocation_id', 'observed_at', 'test_unique_id', 'test_name', 'unique_id',
    'record_values_json', 'failure_row_count', 'initial_poc_responsible', 'initial_call_to_action',
    'test_tags', 'owner_conflict'
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
    {% set meta = node.config.meta or {} %}
    {% set dqm_meta = meta.get('dbt_dqm', {}) %}
    {% set configured_grain = dqm_meta.get('granularity', []) %}
    {% set capture_mode = dqm_meta.get('capture_mode', var('dbt_dqm_capture_mode', 'identity_only')) %}
    {% set context_columns = dqm_meta.get('context_columns', []) %}
    {% set owner_column = dqm_meta.get('owner_column') %}
    {% set priority = dqm_meta.get('priority') %}
    {% set grain_errors = [] %}
    {% if modules.re.sub('\\s+', '', node.config.fail_calc|lower) != 'count(*)' %}
      {% do grain_errors.append('Tracked tests require fail_calc=count(*).') %}
    {% endif %}
    {% if node.config.limit is not none %}{% do grain_errors.append('Tracked tests cannot configure limit.') %}{% endif %}
    {% if node.config.store_failures_as not in [none,'table'] %}{% do grain_errors.append('Tracked tests require table failure storage.') %}{% endif %}
    {% if owner_column is not none and (owner_column is not string or owner_column|trim=='') %}
      {% do grain_errors.append('owner_column must be a nonblank string.') %}
    {% endif %}
    {% if priority is not none and (priority is not string or priority|trim|lower not in ['critical','high','medium','low','unset']) %}
      {% do grain_errors.append('priority must be critical, high, medium or low.') %}
    {% endif %}
    {% if dqm_meta.get('criticality') is not none and dqm_meta.get('criticality') is not string %}
      {% do grain_errors.append('criticality must be a string.') %}
    {% endif %}
    {% set grain_lower = [] %}
    {% if configured_grain is string or configured_grain is not sequence or configured_grain | length == 0 %}
      {% do grain_errors.append('meta.dbt_dqm.granularity must be a non-empty list of column names.') %}
    {% else %}
      {% for grain_name in configured_grain %}
        {% if grain_name is not string or grain_name | trim == '' %}
          {% do grain_errors.append('Every meta.dbt_dqm.granularity entry must be a non-empty string.') %}
        {% else %}
          {% set normalized_name = grain_name | trim | lower %}
          {% if normalized_name in grain_lower %}
            {% do grain_errors.append('meta.dbt_dqm.granularity contains a duplicate column: ' ~ grain_name) %}
          {% else %}
            {% do grain_lower.append(normalized_name) %}
          {% endif %}
        {% endif %}
      {% endfor %}
    {% endif %}
    {% if capture_mode not in ['identity_only', 'allowlist', 'full'] %}
      {% do grain_errors.append(
        "meta.dbt_dqm.capture_mode must be one of: identity_only, allowlist, full."
      ) %}
    {% endif %}
    {% if context_columns is string or context_columns is not sequence %}
      {% do grain_errors.append('meta.dbt_dqm.context_columns must be a list of column names.') %}
    {% else %}
      {% set context_lower = [] %}
      {% for context_name in context_columns %}
        {% if context_name is not string or context_name | trim == '' %}
          {% do grain_errors.append('Every meta.dbt_dqm.context_columns entry must be a non-empty string.') %}
        {% elif context_name | trim | lower in context_lower %}
          {% do grain_errors.append('meta.dbt_dqm.context_columns contains a duplicate column: ' ~ context_name) %}
        {% else %}
          {% do context_lower.append(context_name | trim | lower) %}
        {% endif %}
      {% endfor %}
    {% endif %}

    {% if grain_errors | length > 0 %}
      {% set error_update %}
        update {{ dbt_dqm.relation_name('dqm_test_executions') }}
        set collection_status = {{ dbt_dqm.sql_string('configuration_error') }},
            collection_message = {{ dbt_dqm.sql_string(grain_errors | join(' ')) }}
        where invocation_id = {{ dbt_dqm.sql_string(invocation_id) }}
          and test_unique_id = {{ dbt_dqm.sql_string(node.unique_id) }}
      {% endset %}
      {% do capture_sql.append(error_update ~ ';') %}
      {% do log('dbt-dqm skipped ' ~ node.unique_id ~ ': ' ~ (grain_errors | join(' ')), info=true) %}
    {% elif result.status | lower not in ['pass', 'warn', 'fail'] %}
      {% set inconclusive_update %}
        update {{ dbt_dqm.relation_name('dqm_test_executions') }}
        set collection_status = {{ dbt_dqm.sql_string('inconclusive') }},
            collection_message = {{ dbt_dqm.sql_string('The dbt test result was not conclusive.') }}
        where invocation_id = {{ dbt_dqm.sql_string(invocation_id) }}
          and test_unique_id = {{ dbt_dqm.sql_string(node.unique_id) }}
      {% endset %}
      {% do capture_sql.append(inconclusive_update ~ ';') %}
    {% elif not node.relation_name %}
      {% set relation_error %}
        update {{ dbt_dqm.relation_name('dqm_test_executions') }}
        set collection_status = {{ dbt_dqm.sql_string('collection_error') }},
            collection_message = {{ dbt_dqm.sql_string('The test reported failures but did not expose a stored-failure relation.') }}
        where invocation_id = {{ dbt_dqm.sql_string(invocation_id) }}
          and test_unique_id = {{ dbt_dqm.sql_string(node.unique_id) }}
      {% endset %}
      {% do capture_sql.append(relation_error ~ ';') %}
      {% do log('dbt-dqm could not resolve stored failures for ' ~ node.unique_id, info=true) %}
    {% else %}
      {# Construct directly: dbt's relation cache can be stale immediately after store_failures. #}
      {% set relation = api.Relation.create(
        database=node.database,
        schema=node.schema,
        identifier=node.alias,
        type='table'
      ) %}
      {% set columns = adapter.get_columns_in_relation(relation) %}
      {% if columns | length > 0 %}
        {% set key_columns = [] %}
        {# Configured order is part of the canonical identity and must remain stable. #}
        {% for grain_name in grain_lower %}
          {% for column in columns %}
            {% if column.name | lower == grain_name %}{% do key_columns.append(column) %}{% endif %}
          {% endfor %}
        {% endfor %}
        {% set missing_context_columns = [] %}
        {% if capture_mode == 'allowlist' %}
          {% for context_name in context_lower %}
            {% set context_found = [] %}
            {% for column in columns %}
              {% if column.name | lower == context_name %}{% do context_found.append(column.name) %}{% endif %}
            {% endfor %}
            {% if context_found | length == 0 %}{% do missing_context_columns.append(context_name) %}{% endif %}
          {% endfor %}
        {% endif %}
        {% set resolved_owner = [] %}
        {% for column in columns %}
          {% if column.name|lower == (owner_column|trim|lower if owner_column is string else 'dqm_owner') %}{% do resolved_owner.append(column) %}{% endif %}
        {% endfor %}
        {% if owner_column is not none and resolved_owner|length!=1 %}{% do missing_context_columns.append(owner_column) %}{% endif %}
        {% if resolved_owner|length>1 %}{% do missing_context_columns.append('ambiguous owner column') %}{% endif %}
        {% if key_columns | length != grain_lower | length or missing_context_columns | length > 0 %}
          {% set missing_columns_message = '' %}
          {% if missing_context_columns | length > 0 %}
            {% set missing_columns_message = '; missing context: ' ~ (missing_context_columns | join(', ')) %}
          {% endif %}
          {% set error_update %}
            update {{ dbt_dqm.relation_name('dqm_test_executions') }}
            set collection_status = {{ dbt_dqm.sql_string('configuration_error') }},
                collection_message = {{ dbt_dqm.sql_string(
                  'Every configured granularity/context column must exist in the test output. '
                  ~ 'Granularity: ' ~ (configured_grain | join(', '))
                  ~ missing_columns_message
                ) }}
            where invocation_id = {{ dbt_dqm.sql_string(invocation_id) }}
              and test_unique_id = {{ dbt_dqm.sql_string(node.unique_id) }}
          {% endset %}
          {% do capture_sql.append(error_update ~ ';') %}
          {% do log('dbt-dqm skipped ' ~ node.unique_id ~ ': a granularity column is missing', info=true) %}
        {% elif result.failures is not none and result.failures|int > 0 %}
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
            {% set normalized_column_name = column.name | lower %}
            {% if capture_mode == 'full'
              or normalized_column_name in grain_lower
              or (capture_mode == 'allowlist' and normalized_column_name in context_lower) %}
              {% set value_sql %}
                case when {{ adapter.quote(column.name) }} is null then null
                     else cast({{ adapter.quote(column.name) }} as {{ dbt.type_string() }}) end
              {% endset %}
              {% do record_pairs.append((column.name, value_sql)) %}
            {% endif %}
          {% endfor %}

          {% set owner_value %}{% if resolved_owner %}nullif(trim(cast({{ adapter.quote(resolved_owner[0].name) }} as {{ dbt.type_string() }})),''){% else %}cast(null as {{ dbt.type_string() }}){% endif %}{% endset %}
          {% set query %}
            with failure_rows as (
              select
                {{ dbt_dqm.sha256_hex(dbt_dqm.canonical_identity_string(key_pairs)) }} as unique_id,
                {{ dbt_dqm.json_object_string(record_pairs) }} as record_values_json,
                {{ owner_value }} as owner_value
              from {{ relation }}
            ),
            owner_rollup as (
              select unique_id, count(distinct owner_value) as owner_count, min(owner_value) as owner_value
              from failure_rows group by unique_id
            ),
            deduplicated_failures as (
              select
              {{ dbt_dqm.sql_string(invocation_id) }} as invocation_id,
              {{ dbt.current_timestamp() }} as observed_at,
              {{ dbt_dqm.sql_string(node.unique_id) }} as test_unique_id,
              {{ dbt_dqm.sql_string(node.name) }} as test_name,
              failure_rows.unique_id,
              record_values_json, owner_rollup.owner_value, owner_rollup.owner_count,
              count(*) over (partition by failure_rows.unique_id) as failure_row_count,
              row_number() over (
                partition by failure_rows.unique_id
                order by record_values_json
              ) as representative_rank
              from failure_rows join owner_rollup on failure_rows.unique_id=owner_rollup.unique_id
            )
            select
              invocation_id,
              observed_at,
              test_unique_id,
              test_name,
              unique_id,
              record_values_json,
              failure_row_count,
              case when owner_count>1 then {{ dbt_dqm.sql_string(owner) }} else coalesce(owner_value,{{ dbt_dqm.sql_string(owner) }}) end as initial_poc_responsible,
              {{ dbt_dqm.sql_string(action) }} as initial_call_to_action,
              {{ dbt_dqm.sql_string(tags_json) }} as test_tags,
              case when owner_count>1 then 1 else 0 end as owner_conflict
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
          {% do capture_sql.append('begin transaction;' if target.type=='bigquery' else 'begin;') %}
          {% do capture_sql.append(observation_merge ~ ';') %}
          {% set status_update %}
            update {{ dbt_dqm.relation_name('dqm_test_executions') }}
            set collection_status = {{ dbt_dqm.sql_string('success') }},
                owner_conflict_identity_count=(select coalesce(sum(owner_conflict),0) from {{ dbt_dqm.dqm_relation('dqm_issue_observations') }} where invocation_id={{ dbt_dqm.sql_string(invocation_id) }} and test_unique_id={{ dbt_dqm.sql_string(node.unique_id) }}),
                collection_message=case when exists(select 1 from {{ dbt_dqm.dqm_relation('dqm_issue_observations') }} where invocation_id={{ dbt_dqm.sql_string(invocation_id) }} and test_unique_id={{ dbt_dqm.sql_string(node.unique_id) }} and owner_conflict=1) then 'Conflicting row owners; static owner used.' else null end
            where invocation_id = {{ dbt_dqm.sql_string(invocation_id) }}
              and test_unique_id = {{ dbt_dqm.sql_string(node.unique_id) }}
              and collection_status = {{ dbt_dqm.sql_string('pending') }}
          {% endset %}
          {% do capture_sql.append(status_update ~ ';') %}
          {% do capture_sql.append('commit transaction;' if target.type=='bigquery' else 'commit;') %}
        {% else %}
          {% do successful_test_ids.append(node.unique_id) %}
        {% endif %}
      {% else %}
        {% set relation_error %}
          update {{ dbt_dqm.relation_name('dqm_test_executions') }}
          set collection_status = {{ dbt_dqm.sql_string('collection_error') }},
              collection_message = {{ dbt_dqm.sql_string('The stored-failure relation was not available.') }}
          where invocation_id = {{ dbt_dqm.sql_string(invocation_id) }}
            and test_unique_id = {{ dbt_dqm.sql_string(node.unique_id) }}
        {% endset %}
        {% do capture_sql.append(relation_error ~ ';') %}
        {% do log('dbt-dqm could not read stored failures for ' ~ node.unique_id, info=true) %}
      {% endif %}
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
    {% do capture_sql.append(success_update ~ ';') %}
  {% endif %}
  {{ return(capture_sql|join('\n')) }}
{% endmacro %}
