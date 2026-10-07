-- depends_on: {{ ref('dqm_reconcile') }}
-- depends_on: {{ ref('dqm_missed_issues') }}
{{ config(materialized='view') }}
{% set window=dbt_dqm.positive_integer_var('dbt_dqm_metrics_window_days',30) %}
{% set stale=dbt_dqm.positive_integer_var('dbt_dqm_stale_days',14) %}
with tracked as ({{ dbt_dqm.health_tests_sql() }}),
review as (
  select test_unique_id,count(*) as review_population_count,
    sum(case when review_verdict='TRUE_POSITIVE' then 1 else 0 end) as tp_count,
    sum(case when review_verdict='FALSE_POSITIVE' then 1 else 0 end) as fp_count
  from {{ dbt_dqm.dqm_relation('dqm_issue_occurrences') }}
  where first_seen_at >= {{ dbt_dqm.health_window_start() }} group by test_unique_id
),
backlog as (
  select test_unique_id,count(*) as active_issues,
    avg({{ dbt.datediff('first_seen_at',dbt.current_timestamp(),'hour') }} / 24.0) as avg_active_age_days,
    sum(case when workflow_status='NEW' and first_seen_at < {{ dbt_dqm.timestamp_add('day',-stale,dbt.current_timestamp()) }} then 1 else 0 end) as stale_new_issues,
    sum(case when workflow_status='ACCEPTED_RISK' then 1 else 0 end) as accepted_risk_count
  from {{ dbt_dqm.dqm_relation('dqm_issue_occurrences') }} where record_status='Active' group by test_unique_id
),
resolution as (
  select test_unique_id,count(*) as passed_closure_count,
    avg({{ dbt.datediff('first_seen_at','archived_at','hour') }} * 1.0) as avg_hours_to_pass
  from {{ dbt_dqm.dqm_relation('dqm_issue_occurrences') }}
  where close_reason='Passed' and archived_at >= {{ dbt_dqm.health_window_start() }} group by test_unique_id
),
lifecycle as (
  select test_unique_id,sum(case when is_recurrence then 1 else 0 end) as recurrence_count,
    sum(case when is_potential_regression then 1 else 0 end) as potential_regression_count
  from ({{ dbt_dqm.issues_with_history() }}) history
  where first_seen_at >= {{ dbt_dqm.health_window_start() }} group by test_unique_id
),
executions as (
  select execution.test_unique_id,count(*) as recorded_execution_count,
    sum(case when {{ dbt_dqm.conclusive_sql() }} then 1 else 0 end) as conclusive_execution_count,
    sum(case when receipt.outcome='applied' then 1 else 0 end) as applied_execution_count,
    sum(case when execution.collection_status in ('configuration_error','collection_error','inconclusive') then 1 else 0 end) as collection_problem_count,
    sum(case when execution.collection_status='configuration_error' then 1 else 0 end) as configuration_error_count,
    sum(case when execution.collection_status='collection_error' then 1 else 0 end) as collection_error_count,
    sum(case when execution.collection_status='inconclusive' then 1 else 0 end) as inconclusive_count,
    sum(coalesce(owner_conflict_identity_count,0)) as owner_conflict_identity_count,
    sum(case when failure_count>0 then 1 else 0 end) as failing_execution_count,
    max(execution.captured_at) as latest_window_execution_at
  from {{ dbt_dqm.dqm_relation('dqm_test_executions') }} execution
  left join {{ dbt_dqm.dqm_relation('dqm_reconciliation_receipts') }} receipt using(test_unique_id,invocation_id)
  where execution.captured_at >= {{ dbt_dqm.health_window_start() }} group by execution.test_unique_id
),
outstanding as (
  select execution.test_unique_id,
    sum(case when execution.collection_status='pending' then 1 else 0 end) as pending_collection_count,
    sum(case when {{ dbt_dqm.conclusive_sql() }} and receipt.invocation_id is null then 1 else 0 end) as processing_lag_count,
    min(case when {{ dbt_dqm.conclusive_sql() }} and receipt.invocation_id is null then execution.captured_at end) as oldest_unprocessed_at,
    max(execution.captured_at) as latest_execution_at
  from {{ dbt_dqm.dqm_relation('dqm_test_executions') }} execution
  left join {{ dbt_dqm.dqm_relation('dqm_reconciliation_receipts') }} receipt using(test_unique_id,invocation_id)
  group by execution.test_unique_id
),
gaps as (
  select test_unique_id,count(*) as skipped_evidence_count from {{ dbt_dqm.dqm_relation('dqm_issue_events') }}
  where event_type='EVIDENCE_SKIPPED' and event_at >= {{ dbt_dqm.health_window_start() }} group by test_unique_id
),
misses as (
  select test_unique_id,count(*) as known_missed_issue_count from {{ dbt_dqm.dqm_relation('dqm_missed_issues') }}
  where discovered_at >= {{ dbt_dqm.health_window_start() }} group by test_unique_id
),
counts as (
  select tracked.*,{{ window }} as reporting_window_days,{{ stale }} as stale_threshold_days,
    {{ dbt.current_timestamp() }} as computed_at,
    (select raw_pruned_before from {{ dbt_dqm.dqm_relation('dqm_reconciliation_control') }}) as raw_pruned_before,
    coalesce((select raw_pruned_before from {{ dbt_dqm.dqm_relation('dqm_reconciliation_control') }}) > {{ dbt_dqm.health_window_start() }},false) as retention_limits_evidence,
    {% for name in ['review_population_count','tp_count','fp_count'] %}coalesce(review.{{ name }},0) as {{ name }},{% endfor %}
    {% for name in ['active_issues','stale_new_issues','accepted_risk_count'] %}coalesce(backlog.{{ name }},0) as {{ name }},{% endfor %}
    backlog.avg_active_age_days,coalesce(resolution.passed_closure_count,0) as passed_closure_count,resolution.avg_hours_to_pass,
    coalesce(lifecycle.recurrence_count,0) as recurrence_count,coalesce(lifecycle.potential_regression_count,0) as potential_regression_count,
    {% for name in ['configuration_error_count','collection_error_count','inconclusive_count','recorded_execution_count','conclusive_execution_count','applied_execution_count','collection_problem_count','owner_conflict_identity_count','failing_execution_count'] %}coalesce(executions.{{ name }},0) as {{ name }},{% endfor %}
    coalesce(outstanding.pending_collection_count,0) as pending_collection_count,
    coalesce(outstanding.processing_lag_count,0) as processing_lag_count,
    outstanding.latest_execution_at,outstanding.oldest_unprocessed_at,
    {{ dbt.datediff("outstanding.latest_execution_at",dbt.current_timestamp(),"hour") }} as hours_since_latest_execution,
    coalesce(gaps.skipped_evidence_count,0) as skipped_evidence_count,
    coalesce(misses.known_missed_issue_count,0) as known_missed_issue_count
  from tracked left join review using(test_unique_id) left join backlog using(test_unique_id)
  left join resolution using(test_unique_id) left join lifecycle using(test_unique_id)
  left join executions using(test_unique_id) left join outstanding using(test_unique_id) left join gaps using(test_unique_id) left join misses using(test_unique_id)
), rates as (
  select *,tp_count+fp_count as assessed_count,review_population_count-tp_count-fp_count as unreviewed_count,
    {{ dbt_dqm.safe_rate('tp_count','tp_count+fp_count') }} as reviewed_precision,
    {{ dbt_dqm.safe_rate('fp_count','tp_count+fp_count') }} as fp_share,
    {{ dbt_dqm.safe_rate('tp_count+fp_count','review_population_count') }} as review_coverage,
    {{ dbt_dqm.safe_rate('conclusive_execution_count','recorded_execution_count') }} as collection_coverage,
    {{ dbt_dqm.safe_rate('applied_execution_count','conclusive_execution_count') }} as processing_coverage,
    failing_execution_count=0 and recorded_execution_count>0 as no_observed_failures
  from counts
)
select *,concat(
  case when fp_share>=0.3 and assessed_count>=5 then 'noisy,' else '' end,
  case when stale_new_issues>0 then 'stale_backlog,' else '' end,
  case when collection_problem_count>0 then 'collection_problems,' else '' end,
  case when processing_lag_count>0 then 'lag,' else '' end,
  case when skipped_evidence_count>0 then 'skipped_evidence,' else '' end,
  case when known_missed_issue_count>0 then 'known_misses,' else '' end
) as attention_flags
from rates
