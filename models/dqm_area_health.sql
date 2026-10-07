-- depends_on: {{ ref('dqm_reconcile') }}
-- depends_on: {{ ref('dqm_missed_issues') }}
-- depends_on: {{ ref('dqm_test_health') }}
{{ config(materialized='view') }}
with areas as ({{ dbt_dqm.health_areas_sql() }}),dependencies as ({{ dbt_dqm.health_areas_sql(true) }}),
test_totals as (
  select dependencies.source_unique_id,count(*) as tracked_test_count,
    sum(health.active_issues) as active_issues,sum(health.tp_count) as tp_count,sum(health.fp_count) as fp_count,
    sum(health.review_population_count) as review_population_count,
    sum(health.potential_regression_count) as potential_regression_count,
    sum(health.stale_new_issues) as stale_new_issues,sum(health.collection_problem_count) as collection_problem_count,
    sum(health.processing_lag_count) as processing_lag_count,sum(health.skipped_evidence_count) as skipped_evidence_count
  from dependencies inner join {{ dbt_dqm.dqm_relation('dqm_test_health') }} health using(test_unique_id)
  group by dependencies.source_unique_id
), misses as (
  select source_unique_id,count(*) as known_missed_issue_count from {{ dbt_dqm.dqm_relation('dqm_missed_issues') }}
  where discovered_at >= {{ dbt_dqm.health_window_start() }} and source_unique_id is not null group by source_unique_id
), unmapped as (
  select area_label as area_name,count(*) as known_missed_issue_count from {{ dbt_dqm.dqm_relation('dqm_missed_issues') }}
  where discovered_at >= {{ dbt_dqm.health_window_start() }} and source_unique_id is null group by area_label
), counts as (
 select areas.source_unique_id,areas.area_name,
   {% for col in ['tracked_test_count','active_issues','tp_count','fp_count','review_population_count','potential_regression_count','stale_new_issues','collection_problem_count','processing_lag_count','skipped_evidence_count'] %}coalesce(test_totals.{{ col }},0) as {{ col }},{% endfor %}
   coalesce(misses.known_missed_issue_count,0) as known_missed_issue_count
 from areas left join test_totals using(source_unique_id) left join misses using(source_unique_id)
 union all select cast(null as {{ dbt.type_string() }}),area_name,0,0,0,0,0,0,0,0,0,0,known_missed_issue_count from unmapped
)
select *,{{ dbt_dqm.safe_rate('tp_count','tp_count+fp_count') }} as reviewed_precision,
 {{ dbt_dqm.safe_rate('fp_count','tp_count+fp_count') }} as fp_share,tp_count+fp_count as assessed_count,
 {{ dbt_dqm.positive_integer_var('dbt_dqm_metrics_window_days',30) }} as reporting_window_days,
 {{ dbt.current_timestamp() }} as computed_at,
 concat(case when tracked_test_count=0 then 'no_tracked_coverage,' else '' end,
 case when {{ dbt_dqm.safe_rate('fp_count','tp_count+fp_count') }}>=0.3 and tp_count+fp_count>=5 then 'noisy,' else '' end,
 case when known_missed_issue_count>0 then 'known_misses,' else '' end,
 case when stale_new_issues>0 then 'stale_backlog,' else '' end,
 case when collection_problem_count>0 then 'collection_problems,' else '' end,
 case when processing_lag_count>0 then 'lag,' else '' end,
 case when skipped_evidence_count>0 then 'skipped_evidence,' else '' end) as attention_flags
from counts
