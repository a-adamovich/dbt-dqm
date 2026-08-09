-- Reconcile the latest conclusive test observations into durable issue occurrences.
-- Active failures retain their workflow annotations, conclusive absence archives them as Passed,
-- and a later recurrence creates a new occurrence without copying prior annotations.
{{
  config(
    alias='dqm_issue_occurrences',
    materialized='incremental',
    incremental_strategy='merge',
    unique_key='occurrence_id',
    full_refresh=false,
    on_schema_change='append_new_columns',
    cluster_by=['record_status', 'test_unique_id']
  )
}}

{#
  Tests currently tagged and tracked in this project's manifest, independent of whether they were
  selected/run in this particular invocation. A test's occurrences can only be swept as orphaned
  (close_reason = 'TEST_REMOVED') once it disappears from here entirely; simply not being selected
  in one invocation must never archive its occurrences.
#}
{% set tracked_test_ids = [] %}
{% for node in graph.nodes.values() %}
  {% if node.resource_type == 'test' and dbt_dqm.tracked_test(node) %}
    {% do tracked_test_ids.append(node.unique_id) %}
  {% endif %}
{% endfor %}

with currently_tracked_tests as (
  {% if tracked_test_ids | length > 0 %}
    select test_unique_id from unnest([
      {% for test_id in tracked_test_ids %}
        {{ dbt_dqm.sql_string(test_id) }}{% if not loop.last %},{% endif %}
      {% endfor %}
    ]) as test_unique_id
  {% else %}
    select cast(null as string) as test_unique_id from unnest([1]) where false
  {% endif %}
),

-- Restricted to currently tracked tests: a test's historical dqm_test_executions rows never
-- disappear on their own, so without this filter a removed test's last known execution would
-- keep satisfying still_active/newly_archived forever, racing orphaned_occurrences below for the
-- same occurrence_id. Tracked-but-not-selected-this-invocation tests are unaffected, since this
-- only ever narrows by manifest presence, not by what ran in this particular invocation.
--
-- `dbt_dqm_reconcile_lookback_days` is an opt-in cost lever: unset (the default), this scans the
-- whole history to find each test's latest execution, exactly as before. Set it once you've
-- confirmed the window comfortably exceeds how infrequently your slowest tracked test runs, and
-- BigQuery can prune partitions instead of scanning dqm_test_executions in full on every
-- reconciliation. A test whose last execution falls outside the window simply stops being
-- reconciled until it runs again — its existing occurrences are untouched either way.
latest_executions as (
  select * except(row_number)
  from (
    select
      execution.*,
      row_number() over (
        partition by execution.test_unique_id
        order by execution.captured_at desc, execution.invocation_id desc
      ) as row_number
    from {{ dbt_dqm.relation_name('dqm_test_executions') }} execution
    inner join currently_tracked_tests tracked
      on execution.test_unique_id = tracked.test_unique_id
    where execution.collection_status in ('success', 'not_applicable')
      and lower(execution.test_status) in ('pass', 'warn', 'fail')
      {% set lookback_days = var('dbt_dqm_reconcile_lookback_days', none) %}
      {% if lookback_days is not none %}
      and execution.captured_at >= timestamp_sub(current_timestamp(), interval {{ lookback_days | int }} day)
      {% endif %}
  )
  where row_number = 1
),

latest_observations as (
  select
    observation.*,
    execution.granularity_signature as current_granularity_signature
  from {{ dbt_dqm.relation_name('dqm_issue_observations') }} observation
  inner join latest_executions execution
    on observation.invocation_id = execution.invocation_id
   and observation.test_unique_id = execution.test_unique_id
),

{% if is_incremental() %}
existing as (
  select * from {{ this }}
),
{% else %}
existing as (
  select
    cast(null as string) occurrence_id,
    cast(null as string) test_unique_id,
    cast(null as string) test_name,
    cast(null as string) unique_id,
    cast(null as int64) occurrence_number,
    cast(null as string) record_values_json,
    cast(null as int64) failure_row_count,
    cast(null as string) test_tags,
    cast(null as timestamp) first_seen_at,
    cast(null as timestamp) last_seen_at,
    cast(null as timestamp) archived_at,
    cast(null as string) record_status,
    cast(null as string) close_reason,
    cast(null as string) first_seen_invocation,
    cast(null as string) last_seen_invocation,
    cast(null as string) last_evaluated_invocation,
    cast(null as string) test_status,
    cast(null as string) call_to_action,
    cast(null as string) ticket_url,
    cast(null as string) notes,
    cast(null as string) poc_responsible,
    cast(null as timestamp) annotation_updated_at,
    cast(null as string) granularity_signature
  from unnest([1])
  where false
),
{% endif %}

active_existing as (
  select * from existing where record_status = 'Active'
),

occurrence_counts as (
  select test_unique_id, unique_id, max(occurrence_number) as max_occurrence_number
  from existing
  group by test_unique_id, unique_id
),

still_active as (
  select
    existing.occurrence_id,
    existing.test_unique_id,
    observation.test_name,
    existing.unique_id,
    existing.occurrence_number,
    observation.record_values_json,
    observation.failure_row_count,
    observation.test_tags,
    existing.first_seen_at,
    observation.observed_at as last_seen_at,
    cast(null as timestamp) as archived_at,
    'Active' as record_status,
    cast(null as string) as close_reason,
    existing.first_seen_invocation,
    observation.invocation_id as last_seen_invocation,
    observation.invocation_id as last_evaluated_invocation,
    existing.test_status,
    existing.call_to_action,
    existing.ticket_url,
    existing.notes,
    existing.poc_responsible,
    existing.annotation_updated_at,
    observation.current_granularity_signature as granularity_signature
  from latest_observations observation
  inner join active_existing existing
    on observation.test_unique_id = existing.test_unique_id
   and observation.unique_id = existing.unique_id
),

new_occurrences as (
  select
    lower(to_hex(sha256(concat(
      observation.test_unique_id, '|', observation.unique_id, '|', observation.invocation_id
    )))) as occurrence_id,
    observation.test_unique_id,
    observation.test_name,
    observation.unique_id,
    coalesce(counts.max_occurrence_number, 0) + 1 as occurrence_number,
    observation.record_values_json,
    observation.failure_row_count,
    observation.test_tags,
    observation.observed_at as first_seen_at,
    observation.observed_at as last_seen_at,
    cast(null as timestamp) as archived_at,
    'Active' as record_status,
    cast(null as string) as close_reason,
    observation.invocation_id as first_seen_invocation,
    observation.invocation_id as last_seen_invocation,
    observation.invocation_id as last_evaluated_invocation,
    'NEW' as test_status,
    observation.initial_call_to_action as call_to_action,
    cast(null as string) as ticket_url,
    cast(null as string) as notes,
    observation.initial_poc_responsible as poc_responsible,
    cast(null as timestamp) as annotation_updated_at,
    observation.current_granularity_signature as granularity_signature
  from latest_observations observation
  left join active_existing existing
    on observation.test_unique_id = existing.test_unique_id
   and observation.unique_id = existing.unique_id
  left join occurrence_counts counts
    on observation.test_unique_id = counts.test_unique_id
   and observation.unique_id = counts.unique_id
  where existing.occurrence_id is null
),

-- A conclusive execution ran for this test this invocation, and the occurrence's identity was not
-- among its observations. That's a genuine pass only if the test's granularity is unchanged since
-- this occurrence was last touched; if the signature differs, the old identity may simply no
-- longer exist under the new grain, so it is archived as unresolved rather than falsely "Passed".
newly_archived as (
  select
    existing.occurrence_id,
    existing.test_unique_id,
    existing.test_name,
    existing.unique_id,
    existing.occurrence_number,
    existing.record_values_json,
    existing.failure_row_count,
    existing.test_tags,
    existing.first_seen_at,
    existing.last_seen_at,
    execution.captured_at as archived_at,
    'Archived' as record_status,
    case
      when existing.granularity_signature is not null
       and execution.granularity_signature is not null
       and existing.granularity_signature != execution.granularity_signature
      then 'IDENTITY_CHANGED'
      else 'Passed'
    end as close_reason,
    existing.first_seen_invocation,
    existing.last_seen_invocation,
    execution.invocation_id as last_evaluated_invocation,
    existing.test_status,
    existing.call_to_action,
    existing.ticket_url,
    existing.notes,
    existing.poc_responsible,
    existing.annotation_updated_at,
    existing.granularity_signature
  from active_existing existing
  inner join latest_executions execution
    on existing.test_unique_id = execution.test_unique_id
   and existing.last_evaluated_invocation != execution.invocation_id
  left join latest_observations observation
    on existing.test_unique_id = observation.test_unique_id
   and existing.unique_id = observation.unique_id
  where observation.unique_id is null
),

-- The test itself has been renamed or removed from the project (absent from the current manifest
-- entirely, not merely unselected in this invocation). latest_executions/latest_observations above
-- are both restricted to currently tracked tests, so a removed test's occurrences can never be
-- claimed by still_active, new_occurrences, or newly_archived — this is the only remaining path
-- for them, and it's mutually exclusive with the other three by construction.
orphaned_occurrences as (
  select
    existing.occurrence_id,
    existing.test_unique_id,
    existing.test_name,
    existing.unique_id,
    existing.occurrence_number,
    existing.record_values_json,
    existing.failure_row_count,
    existing.test_tags,
    existing.first_seen_at,
    existing.last_seen_at,
    current_timestamp() as archived_at,
    'Archived' as record_status,
    'TEST_REMOVED' as close_reason,
    existing.first_seen_invocation,
    existing.last_seen_invocation,
    {{ dbt_dqm.sql_string(invocation_id) }} as last_evaluated_invocation,
    existing.test_status,
    existing.call_to_action,
    existing.ticket_url,
    existing.notes,
    existing.poc_responsible,
    existing.annotation_updated_at,
    existing.granularity_signature
  from active_existing existing
  left join currently_tracked_tests tracked
    on existing.test_unique_id = tracked.test_unique_id
  where tracked.test_unique_id is null
)

select * from still_active
union all
select * from new_occurrences
union all
select * from newly_archived
union all
select * from orphaned_occurrences
