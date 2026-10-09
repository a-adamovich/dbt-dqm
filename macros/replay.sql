{% macro reconcile_change_set_sql(run_id, observation_floor=none) %}
{% set occurrences = dbt_dqm.dqm_relation('dqm_issue_occurrences') %}
{% set occurrence_columns %}{% for col, kind in dbt_dqm.table_schemas()['dqm_issue_occurrences'] %}{{ col }}{% if not loop.last %},{% endif %}{% endfor %}{% endset %}
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

{#- Only Active rows and the history of identities this run touches are read. Archived history
    of untouched identities never affects the change set, and reading the whole table made every
    run scale with total history. #}
{#- Active rows that this run can change: those of tests with frozen inputs, plus those of tests
    no longer tracked (the TEST_REMOVED sweep). Other tests' Active rows can't produce states,
    because states join each identity to executions of its own test. #}
active_existing as (
  select {{ occurrence_columns }} from {{ occurrences }} active
  where record_status = 'Active'
    and (
      exists(
        select 1 from {{ dbt_dqm.dqm_relation('dqm_reconciliation_inputs') }} frozen
        where frozen.run_id = {{ dbt_dqm.sql_string(run_id) }}
          and frozen.test_unique_id = active.test_unique_id)
      or not exists(
        select 1 from currently_tracked_tests tracked
        where tracked.test_unique_id = active.test_unique_id)
    )
),

unprocessed_executions as (
  select execution.* from {{ dbt_dqm.dqm_relation('dqm_test_executions') }} execution
  inner join {{ dbt_dqm.dqm_relation('dqm_reconciliation_inputs') }} frozen
    on execution.test_unique_id=frozen.test_unique_id and execution.invocation_id=frozen.invocation_id
  where frozen.run_id={{ dbt_dqm.sql_string(run_id) }}
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
  {#- A constant lower bound lets BigQuery prune observation partitions; it never excludes
      evidence, because observations are written after their execution row (see reconcile_pre). #}
  {% if observation_floor is not none %}where observation.observed_at >= {{ observation_floor }}{% endif %}
),

candidate_identities as (
  select distinct test_unique_id, unique_id, execution_identity_scheme_signature as identity_scheme_signature
  from new_observations
  union distinct
  select distinct test_unique_id, unique_id, identity_scheme_signature
  from active_existing
),

affected_identities as (
  select distinct test_unique_id, unique_id from candidate_identities
),

affected_history as (
  select {% for col, kind in dbt_dqm.table_schemas()['dqm_issue_occurrences'] %}history.{{ col }}{% if not loop.last %},{% endif %}{% endfor %}
  from {{ occurrences }} history
  inner join affected_identities affected
    on history.test_unique_id = affected.test_unique_id and history.unique_id = affected.unique_id
),

occurrence_counts as (
  select test_unique_id, unique_id, max(occurrence_number) as max_occurrence_number
  from affected_history
  group by test_unique_id, unique_id
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
    execution.test_priority, execution.test_criticality, execution.capture_mode,
    execution.test_tags as execution_tags,
    active.record_values_json as prior_payload,
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
    end as previous_failing,
    case when row_number() over (partition by test_unique_id, unique_id, identity_scheme_signature order by execution_sequence)=1 then prior_payload
      else lag(record_values_json) over (partition by test_unique_id, unique_id, identity_scheme_signature order by execution_sequence) end as previous_payload
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

bounded_states as (
  select state.*, min(case when is_failing=0 then execution_sequence end) over (
    partition by test_unique_id,unique_id,identity_scheme_signature,episode_group) as first_absence_sequence
  from states_grouped state where episode_group>0 or prior_active=1
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
  from bounded_states state
  where first_absence_sequence is null or execution_sequence<=first_absence_sequence
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
    max(case when last_execution_rank = 1 then invocation_id end) as last_execution_invocation,
    max(case when last_execution_rank = 1 then test_priority end) as latest_priority,
    max(case when last_execution_rank = 1 then test_criticality end) as latest_criticality,
    max(case when last_execution_rank = 1 then execution_tags end) as latest_tags
  from ranked_episode_states
  group by test_unique_id, unique_id, identity_scheme_signature, episode_group
),

episode_occurrences as (
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
    episode.latest_tags as test_tags,
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
    episode.identity_scheme_signature,
    episode.latest_priority as test_priority,
    episode.latest_criticality as test_criticality,
    case when episode.episode_group=0 then active.review_verdict else 'UNREVIEWED' end as review_verdict,
    case when episode.first_absence_at is null then cast(null as {{ dbt.type_string() }})
      when episode.episode_group=0 then active.workflow_status else 'NEW' end as workflow_status_at_close,
    episode.episode_group
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
    existing.identity_scheme_signature,
    existing.test_priority, existing.test_criticality, existing.review_verdict,
    existing.workflow_status as workflow_status_at_close
  from active_existing existing
  left join currently_tracked_tests tracked
    on existing.test_unique_id = tracked.test_unique_id
  where tracked.test_unique_id is null
)
,
occurrence_changes as (
  select {% for col, kind in dbt_dqm.table_schemas()['dqm_issue_occurrences'] %}{{ col }}{% if not loop.last %},{% endif %}{% endfor %} from episode_occurrences
  union all select * from orphaned_occurrences
),
all_history as (
  select * from affected_history where record_status='Archived'
  union all select * from occurrence_changes
),
transition_events as (
  select state.test_unique_id, episode.occurrence_id, state.unique_id, state.invocation_id,
    state.captured_at as event_at,
    case
      when state.is_failing=1 and state.previous_failing=0 then
        case when prev.close_reason='Passed' then 'REAPPEARED' else 'APPEARED' end
      when state.is_failing=0 and state.previous_failing=1 then
        case when state.identity_scheme_signature is distinct from state.execution_identity_scheme_signature
          then 'CLOSED_STRUCTURAL' else 'DISAPPEARED' end
      when state.is_failing=1 and state.previous_failing=1 and state.capture_mode in ('full','allowlist')
        and state.record_values_json is distinct from state.previous_payload then 'VALUES_CHANGED'
      else 'STILL_FAILING' end as event_type,
    state.previous_payload as previous_record_values_json,
    coalesce(state.record_values_json,state.previous_payload) as record_values_json,
    case when state.is_failing=0 and state.previous_failing=1 then episode.close_reason end as reason
  from states_grouped state
  inner join episode_occurrences episode on state.test_unique_id=episode.test_unique_id
    and state.unique_id=episode.unique_id
    and state.identity_scheme_signature is not distinct from episode.identity_scheme_signature
    and state.episode_group=episode.episode_group
  left join all_history prev on episode.test_unique_id=prev.test_unique_id and episode.unique_id=prev.unique_id
    and episode.identity_scheme_signature is not distinct from prev.identity_scheme_signature
    and prev.occurrence_number=episode.occurrence_number-1
  where (state.is_failing=1 and state.previous_failing=0)
     or (state.is_failing=0 and state.previous_failing=1)
     or (state.is_failing=1 and state.previous_failing=1 and (
          (state.capture_mode in ('full','allowlist') and state.record_values_json is distinct from state.previous_payload)
          {% if var('dbt_dqm_emit_still_failing_events',false) %}or true{% endif %}))
),
event_changes_full as (
  select *, cast(null as {{ dbt.type_string() }}) as actor from transition_events
  union all
  select test_unique_id, occurrence_id, unique_id, {{ dbt_dqm.sql_string(run_id) }}, archived_at,
    'CLOSED_STRUCTURAL', record_values_json, record_values_json, close_reason,
    cast(null as {{ dbt.type_string() }}) from orphaned_occurrences
),
{#- Lifecycle is computed from full payloads; only what the event ledger keeps depends on
    dbt_dqm_event_payloads. #}
{% set payload_mode = dbt_dqm.event_payload_mode() %}
event_changes as (
  select test_unique_id, occurrence_id, unique_id, invocation_id, event_at, event_type,
    {% if payload_mode == 'full' %}previous_record_values_json{% else %}cast(null as {{ dbt.type_string() }}){% endif %} as previous_record_values_json,
    {% if payload_mode == 'full' %}record_values_json{% else %}cast(null as {{ dbt.type_string() }}){% endif %} as record_values_json,
    reason, actor,
    {{ dbt_dqm.sql_string(payload_mode) }} as payload_mode,
    {% if payload_mode == 'none' %}cast(null as {{ dbt.type_string() }})
    {% else %}case when event_type = 'VALUES_CHANGED'
      then {{ dbt_dqm.json_changed_keys('previous_record_values_json', 'record_values_json') }} end{% endif %} as changed_columns,
    {% if payload_mode == 'none' %}cast(null as {{ dbt.type_string() }})
    {% else %}case when previous_record_values_json is not null then {{ dbt_dqm.sha256_hex('previous_record_values_json') }} end{% endif %} as previous_payload_digest,
    {% if payload_mode == 'none' %}cast(null as {{ dbt.type_string() }})
    {% else %}case when record_values_json is not null then {{ dbt_dqm.sha256_hex('record_values_json') }} end{% endif %} as payload_digest
  from event_changes_full
)
select 'occurrence' as row_kind, changes.*,
 {% for col,kind in dbt_dqm.table_schemas()['dqm_issue_events'] if col not in ['test_unique_id','occurrence_id','unique_id','record_values_json'] %}
   cast(null as {{ context['dbt']['type_' ~ kind]() }}) as {{ col }}{% if not loop.last %},{% endif %}
 {% endfor %}
from occurrence_changes changes
union all
select 'event' as row_kind,
 {% for col,kind in dbt_dqm.table_schemas()['dqm_issue_occurrences'] %}
   {% if col in ['test_unique_id','occurrence_id','unique_id','record_values_json'] %}events.{{ col }}{% else %}cast(null as {{ context['dbt']['type_' ~ kind]() }}){% endif %},
 {% endfor %}
 {% for col,kind in dbt_dqm.table_schemas()['dqm_issue_events'] if col not in ['test_unique_id','occurrence_id','unique_id','record_values_json'] %}
   {% if col=='event_id' %}{{ dbt_dqm.event_id_sql('events.test_unique_id','events.occurrence_id','events.invocation_id','events.event_type') }}{% else %}events.{{ col }}{% endif %}{% if not loop.last %},{% endif %}
 {% endfor %}
from event_changes events

{% endmacro %}
