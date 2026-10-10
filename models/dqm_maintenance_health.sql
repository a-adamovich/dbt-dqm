-- Optional-maintenance health, one row per scheduled step (macros/maintenance.sql).
--   ok       the latest outcome is a success
--   failing  the latest outcome is a failure (it is retried on the next maintenance run)
--   unknown  no outcome recorded, or none since the latest DQM execution: missing
--            observations (for example after the outcome log couldn't be written) are never
--            reported as healthy
-- depends_on: {{ ref('dqm_reconcile') }}
{{ config(materialized='view') }}
{% set steps = dbt_dqm.maintenance_steps(
  dbt_dqm.positive_integer_var('dbt_dqm_retention_days', none),
  dbt_dqm.positive_integer_var('dbt_dqm_event_retention_days', none)) %}
{% set log = dbt_dqm.dqm_relation('dqm_maintenance_log') %}
with expected as (
  {% for step in steps %}
    select {{ dbt_dqm.sql_string(step.name) }} as step
    {% if not loop.last %}union all{% endif %}
  {% else %}
    select cast(null as {{ dbt.type_string() }}) as step from {{ dbt_dqm.dual() }} where false
  {% endfor %}
),
outcomes as (
  select step,
    max(case when outcome = 'succeeded' then logged_at end) as last_success_at,
    max(case when outcome = 'failed' then logged_at end) as last_failure_at
  from {{ log }}
  group by step
),
latest_failure as (
  select step, diagnostic_id from (
    select step, diagnostic_id,
      row_number() over (partition by step order by logged_at desc) as recency
    from {{ log }} where outcome = 'failed'
  ) ranked where recency = 1
),
activity as (
  select max(captured_at) as last_dqm_activity_at
  from {{ dbt_dqm.dqm_relation('dqm_test_executions') }}
),
assessed as (
  select expected.step, outcomes.last_success_at, outcomes.last_failure_at,
    latest_failure.diagnostic_id as last_failure_diagnostic_id, activity.last_dqm_activity_at,
    case
      when outcomes.last_success_at is null then outcomes.last_failure_at
      when outcomes.last_failure_at is null then outcomes.last_success_at
      when outcomes.last_success_at > outcomes.last_failure_at then outcomes.last_success_at
      else outcomes.last_failure_at
    end as last_outcome_at
  from expected
  left join outcomes on outcomes.step = expected.step
  left join latest_failure on latest_failure.step = expected.step
  cross join activity
)
select step, last_success_at, last_failure_at, last_failure_diagnostic_id, last_dqm_activity_at,
  case
    when last_outcome_at is null then 'unknown'
    when last_dqm_activity_at is not null and last_outcome_at < last_dqm_activity_at then 'unknown'
    when last_failure_at is not null and (last_success_at is null or last_failure_at > last_success_at) then 'failing'
    else 'ok'
  end as state
from assessed
