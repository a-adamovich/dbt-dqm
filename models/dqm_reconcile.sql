-- Replay every unprocessed conclusive execution into durable issue occurrences. A checkpoint is
-- advanced only by the model post-hook, after this incremental merge succeeds; deterministic
-- occurrence IDs make a replay after an ambiguous failure idempotent.
{{
  config(
    alias='dqm_issue_occurrences',
    materialized='incremental',
    incremental_strategy=dbt_dqm.incremental_upsert_strategy(),
    unique_key='occurrence_id',
    full_refresh=false,
    on_schema_change='append_new_columns',
    cluster_by=['record_status', 'test_unique_id'] if target.type == 'bigquery' else none
  )
}}

{% set tracked_test_ids = [] %}
{% for node in graph.nodes.values() %}
  {% if node.resource_type == 'test' and dbt_dqm.tracked_test(node) %}
    {% do tracked_test_ids.append(node.unique_id) %}
  {% endif %}
{% endfor %}

with currently_tracked_tests as (
  {% if tracked_test_ids | length > 0 %}
    {% for test_id in tracked_test_ids %}
      select {{ dbt_dqm.sql_string(test_id) }} as test_unique_id
      {% if not loop.last %}union all{% endif %}
    {% endfor %}
  {% else %}
    select cast(null as {{ dbt.type_string() }}) as test_unique_id
    from {{ dbt_dqm.dual() }} where false
  {% endif %}
),

{% if is_incremental() %}
existing as (
  select * from {{ this }}
),
{% else %}
existing as (
  select
    cast(null as {{ dbt.type_string() }}) occurrence_id,
    cast(null as {{ dbt.type_string() }}) test_unique_id,
    cast(null as {{ dbt.type_string() }}) test_name,
    cast(null as {{ dbt.type_string() }}) unique_id,
    cast(null as {{ dbt.type_int() }}) occurrence_number,
    cast(null as {{ dbt.type_string() }}) record_values_json,
    cast(null as {{ dbt.type_int() }}) failure_row_count,
    cast(null as {{ dbt.type_string() }}) test_tags,
    cast(null as {{ dbt.type_string() }}) source_unique_id,
    cast(null as {{ dbt.type_string() }}) source_relation,
    cast(null as {{ dbt.type_string() }}) test_severity,
    cast(null as {{ dbt.type_timestamp() }}) first_seen_at,
    cast(null as {{ dbt.type_timestamp() }}) last_seen_at,
    cast(null as {{ dbt.type_timestamp() }}) archived_at,
    cast(null as {{ dbt.type_string() }}) record_status,
    cast(null as {{ dbt.type_string() }}) close_reason,
    cast(null as {{ dbt.type_string() }}) first_seen_invocation,
    cast(null as {{ dbt.type_string() }}) last_seen_invocation,
    cast(null as {{ dbt.type_string() }}) last_evaluated_invocation,
    cast(null as {{ dbt.type_string() }}) workflow_status,
    cast(null as {{ dbt.type_string() }}) test_status,
    cast(null as {{ dbt.type_string() }}) call_to_action,
    cast(null as {{ dbt.type_string() }}) ticket_url,
    cast(null as {{ dbt.type_string() }}) notes,
    cast(null as {{ dbt.type_string() }}) poc_responsible,
    cast(null as {{ dbt.type_timestamp() }}) annotation_updated_at,
    cast(null as {{ dbt.type_int() }}) annotation_version,
    cast(null as {{ dbt.type_string() }}) granularity_signature,
    cast(null as {{ dbt.type_string() }}) identity_scheme_signature
  from {{ dbt_dqm.dual() }} where false
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

conclusive_executions as (
  select execution.*
  from {{ dbt_dqm.relation_name('dqm_test_executions') }} execution
  inner join currently_tracked_tests tracked
    on execution.test_unique_id = tracked.test_unique_id
  where execution.collection_status in ('success', 'not_applicable')
    and lower(execution.test_status) in ('pass', 'warn', 'fail')
    {% set lookback_days = dbt_dqm.positive_integer_var('dbt_dqm_reconcile_lookback_days', none) %}
    {% if lookback_days is not none %}
      and execution.captured_at >= {{ dbt.dateadd('day', -1 * (lookback_days | int), dbt.current_timestamp()) }}
    {% endif %}
),

unprocessed_executions as (
  select execution.*
  from conclusive_executions execution
  left join {{ dbt_dqm.relation_name('dqm_reconciliation_state') }} state
    on execution.test_unique_id = state.test_unique_id
  where state.test_unique_id is null
     or execution.captured_at > state.captured_at
     or (execution.captured_at = state.captured_at and execution.invocation_id > state.invocation_id)
),

ordered_executions as (
  select
    execution.*,
    row_number() over (
      partition by execution.test_unique_id
      order by execution.captured_at, execution.invocation_id
    ) as execution_sequence
  from unprocessed_executions execution
),

new_observations as (
  select
    observation.*,
    execution.execution_sequence,
    execution.granularity_signature as execution_granularity_signature,
    execution.identity_scheme_signature as execution_identity_scheme_signature
  from {{ dbt_dqm.relation_name('dqm_issue_observations') }} observation
  inner join ordered_executions execution
    on observation.invocation_id = execution.invocation_id
   and observation.test_unique_id = execution.test_unique_id
),

candidate_identities as (
  select distinct test_unique_id, unique_id, execution_identity_scheme_signature as identity_scheme_signature
  from new_observations
  union distinct
  select distinct test_unique_id, unique_id, identity_scheme_signature
  from active_existing
),

execution_identity_states as (
  select
    execution.test_unique_id,
    candidate.unique_id,
    candidate.identity_scheme_signature,
    execution.execution_sequence,
    execution.invocation_id,
    execution.captured_at,
    execution.granularity_signature as execution_granularity_signature,
    execution.identity_scheme_signature as execution_identity_scheme_signature,
    execution.source_unique_id,
    execution.source_relation,
    execution.test_severity,
    observation.test_name,
    observation.record_values_json,
    observation.failure_row_count,
    observation.test_tags,
    observation.initial_poc_responsible,
    observation.initial_call_to_action,
    observation.observed_at,
    case when observation.unique_id is null then 0 else 1 end as is_failing,
    case when active.occurrence_id is null then 0 else 1 end as prior_active
  from ordered_executions execution
  inner join candidate_identities candidate
    on execution.test_unique_id = candidate.test_unique_id
  left join new_observations observation
    on execution.invocation_id = observation.invocation_id
   and candidate.test_unique_id = observation.test_unique_id
   and candidate.unique_id = observation.unique_id
   and candidate.identity_scheme_signature is not distinct from observation.execution_identity_scheme_signature
  left join active_existing active
    on candidate.test_unique_id = active.test_unique_id
   and candidate.unique_id = active.unique_id
   and candidate.identity_scheme_signature is not distinct from active.identity_scheme_signature
),

states_with_previous as (
  select
    state.*,
    case
      when row_number() over (
        partition by test_unique_id, unique_id, identity_scheme_signature
        order by execution_sequence
      ) = 1 then prior_active
      else lag(is_failing) over (
        partition by test_unique_id, unique_id, identity_scheme_signature
        order by execution_sequence
      )
    end as previous_failing
  from execution_identity_states state
),

states_grouped as (
  select
    state.*,
    sum(case when is_failing = 1 and previous_failing = 0 then 1 else 0 end) over (
      partition by test_unique_id, unique_id, identity_scheme_signature
      order by execution_sequence rows between unbounded preceding and current row
    ) as episode_group
  from states_with_previous state
),

ranked_episode_states as (
  select
    state.*,
    row_number() over (
      partition by test_unique_id, unique_id, identity_scheme_signature, episode_group
      order by case when is_failing = 1 then 0 else 1 end, execution_sequence
    ) as first_failure_rank,
    row_number() over (
      partition by test_unique_id, unique_id, identity_scheme_signature, episode_group
      order by case when is_failing = 1 then 0 else 1 end, execution_sequence desc
    ) as last_failure_rank,
    row_number() over (
      partition by test_unique_id, unique_id, identity_scheme_signature, episode_group
      order by case when is_failing = 0 then 0 else 1 end, execution_sequence
    ) as first_absence_rank,
    row_number() over (
      partition by test_unique_id, unique_id, identity_scheme_signature, episode_group
      order by execution_sequence desc
    ) as last_execution_rank
  from states_grouped state
  where episode_group > 0 or prior_active = 1
),

episode_rollup as (
  select
    test_unique_id,
    unique_id,
    identity_scheme_signature,
    episode_group,
    max(prior_active) as prior_active,
    max(case when first_failure_rank = 1 and is_failing = 1 then test_name end) as first_test_name,
    max(case when first_failure_rank = 1 and is_failing = 1 then invocation_id end) as first_failure_invocation,
    max(case when first_failure_rank = 1 and is_failing = 1 then observed_at end) as first_failure_at,
    max(case when first_failure_rank = 1 and is_failing = 1 then initial_poc_responsible end) as initial_poc_responsible,
    max(case when first_failure_rank = 1 and is_failing = 1 then initial_call_to_action end) as initial_call_to_action,
    max(case when first_failure_rank = 1 and is_failing = 1 then execution_granularity_signature end) as first_granularity_signature,
    max(case when first_failure_rank = 1 and is_failing = 1 then source_unique_id end) as first_source_unique_id,
    max(case when first_failure_rank = 1 and is_failing = 1 then source_relation end) as first_source_relation,
    max(case when first_failure_rank = 1 and is_failing = 1 then test_severity end) as first_test_severity,
    max(case when last_failure_rank = 1 and is_failing = 1 then record_values_json end) as last_record_values_json,
    max(case when last_failure_rank = 1 and is_failing = 1 then failure_row_count end) as last_failure_row_count,
    max(case when last_failure_rank = 1 and is_failing = 1 then test_tags end) as last_test_tags,
    max(case when last_failure_rank = 1 and is_failing = 1 then observed_at end) as last_failure_at,
    max(case when last_failure_rank = 1 and is_failing = 1 then invocation_id end) as last_failure_invocation,
    max(case when first_absence_rank = 1 and is_failing = 0 then captured_at end) as first_absence_at,
    max(case when first_absence_rank = 1 and is_failing = 0 then invocation_id end) as first_absence_invocation,
    max(case when first_absence_rank = 1 and is_failing = 0 then execution_identity_scheme_signature end) as absence_identity_scheme_signature,
    max(case when last_execution_rank = 1 then invocation_id end) as last_execution_invocation
  from ranked_episode_states
  group by test_unique_id, unique_id, identity_scheme_signature, episode_group
),

replayed_occurrences as (
  select
    case
      when episode.episode_group = 0 then active.occurrence_id
      else {{ dbt_dqm.sha256_hex(
        "concat(episode.test_unique_id, '|', episode.unique_id, '|', episode.first_failure_invocation)"
      ) }}
    end as occurrence_id,
    episode.test_unique_id,
    coalesce(episode.first_test_name, active.test_name) as test_name,
    episode.unique_id,
    case
      when episode.episode_group = 0 then active.occurrence_number
      else coalesce(counts.max_occurrence_number, 0) + episode.episode_group
    end as occurrence_number,
    coalesce(episode.last_record_values_json, active.record_values_json) as record_values_json,
    coalesce(episode.last_failure_row_count, active.failure_row_count) as failure_row_count,
    coalesce(episode.last_test_tags, active.test_tags) as test_tags,
    coalesce(episode.first_source_unique_id, active.source_unique_id) as source_unique_id,
    coalesce(episode.first_source_relation, active.source_relation) as source_relation,
    coalesce(episode.first_test_severity, active.test_severity) as test_severity,
    coalesce(active.first_seen_at, episode.first_failure_at) as first_seen_at,
    coalesce(episode.last_failure_at, active.last_seen_at) as last_seen_at,
    episode.first_absence_at as archived_at,
    case when episode.first_absence_at is null then 'Active' else 'Archived' end as record_status,
    case
      when episode.first_absence_at is null then cast(null as {{ dbt.type_string() }})
      when episode.identity_scheme_signature is null
        or episode.absence_identity_scheme_signature is null
        or episode.identity_scheme_signature != episode.absence_identity_scheme_signature
      then 'IDENTITY_CHANGED'
      else 'Passed'
    end as close_reason,
    coalesce(active.first_seen_invocation, episode.first_failure_invocation) as first_seen_invocation,
    coalesce(episode.last_failure_invocation, active.last_seen_invocation) as last_seen_invocation,
    case
      when episode.first_absence_at is not null then episode.first_absence_invocation
      else episode.last_execution_invocation
    end as last_evaluated_invocation,
    case when episode.episode_group = 0 then coalesce(active.workflow_status, active.test_status, 'NEW') else 'NEW' end as workflow_status,
    case when episode.episode_group = 0 then coalesce(active.workflow_status, active.test_status, 'NEW') else 'NEW' end as test_status,
    case when episode.episode_group = 0 then active.call_to_action else episode.initial_call_to_action end as call_to_action,
    case when episode.episode_group = 0 then active.ticket_url else cast(null as {{ dbt.type_string() }}) end as ticket_url,
    case when episode.episode_group = 0 then active.notes else cast(null as {{ dbt.type_string() }}) end as notes,
    case when episode.episode_group = 0 then active.poc_responsible else episode.initial_poc_responsible end as poc_responsible,
    case when episode.episode_group = 0 then active.annotation_updated_at else cast(null as {{ dbt.type_timestamp() }}) end as annotation_updated_at,
    case when episode.episode_group = 0 then coalesce(active.annotation_version, 0) else 0 end as annotation_version,
    coalesce(episode.first_granularity_signature, active.granularity_signature) as granularity_signature,
    episode.identity_scheme_signature
  from episode_rollup episode
  left join active_existing active
    on episode.test_unique_id = active.test_unique_id
   and episode.unique_id = active.unique_id
   and episode.identity_scheme_signature is not distinct from active.identity_scheme_signature
   and episode.episode_group = 0
  left join occurrence_counts counts
    on episode.test_unique_id = counts.test_unique_id
   and episode.unique_id = counts.unique_id
),

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
    existing.source_unique_id,
    existing.source_relation,
    existing.test_severity,
    existing.first_seen_at,
    existing.last_seen_at,
    {{ dbt.current_timestamp() }} as archived_at,
    'Archived' as record_status,
    'TEST_REMOVED' as close_reason,
    existing.first_seen_invocation,
    existing.last_seen_invocation,
    {{ dbt_dqm.sql_string(invocation_id) }} as last_evaluated_invocation,
    existing.workflow_status,
    existing.test_status,
    existing.call_to_action,
    existing.ticket_url,
    existing.notes,
    existing.poc_responsible,
    existing.annotation_updated_at,
    existing.annotation_version,
    existing.granularity_signature,
    existing.identity_scheme_signature
  from active_existing existing
  left join currently_tracked_tests tracked
    on existing.test_unique_id = tracked.test_unique_id
  where tracked.test_unique_id is null
)

select * from replayed_occurrences
union all
select * from orphaned_occurrences
