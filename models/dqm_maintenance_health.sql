-- Optional-maintenance health, one row per scheduled step (macros/maintenance.sql).
--
-- Triggers: every invocation that captured tracked-test results or completed a reconciliation
-- runs maintenance. Its marker is the later of its latest captured execution and its completed
-- run's start. Outcome rows carry the marker of the invocation that wrote them (read once, before
-- any step); manual cleanups are ordered by their log time.
--
--   ok       the latest assessed outcome (by trigger marker) succeeded, and every trigger at or
--            after it has an outcome
--   failing  an outcome at or after that baseline failed (with no success yet: any recorded
--            failure)
--   unknown  no attributed outcome; a trigger at or after the baseline has no outcome for the
--            step; conflicting outcomes for one invocation; or an unattributed row (written before
--            migration 0006, or whose marker couldn't be read) that no later attributed outcome
--            supersedes
--
-- `ok` is a monitoring approximation, not proof that all eligible work is clean. Ordering follows
-- trigger markers, which on Postgres are transaction-start times: an older-marker invocation can
-- still be running, and a later-marker success supersedes an older trigger's missing outcome.
-- Missing observations are never reported as healthy.
-- depends_on: {{ ref('dqm_reconcile') }}
{{ config(materialized='view') }}
{% set steps = dbt_dqm.maintenance_steps(
  dbt_dqm.positive_integer_var('dbt_dqm_retention_days', none),
  dbt_dqm.positive_integer_var('dbt_dqm_event_retention_days', none)) %}
with expected as (
  {% for step in steps %}
    select {{ dbt_dqm.sql_string(step.name) }} as step
    {% if not loop.last %}union all{% endif %}
  {% else %}
    select cast(null as {{ dbt.type_string() }}) as step from {{ dbt_dqm.dual() }} where false
  {% endfor %}
),
outcome_rows as (
  select step, invocation_id, outcome, logged_at, diagnostic_id,
    case
      when trigger_kind = 'manual' then logged_at
      when trigger_kind = 'automatic' then trigger_marker_at
    end as order_at
  from {{ dbt_dqm.dqm_relation('dqm_maintenance_log') }}
),
-- One assessment per step and invocation. Mixed outcomes are ambiguous; any unattributed row
-- leaves the invocation unattributed.
invocation_outcomes as (
  select step, invocation_id,
    case when min(outcome) = max(outcome) then min(outcome) else 'ambiguous' end as outcome,
    case when count(*) = count(order_at) then max(order_at) end as order_at,
    max(logged_at) as logged_at
  from outcome_rows
  group by step, invocation_id
),
triggers as (
  select invocation_id, max(marker_at) as marker_at from (
    select invocation_id, max(captured_at) as marker_at
    from {{ dbt_dqm.dqm_relation('dqm_test_executions') }}
    group by invocation_id
    union all
    select run_id as invocation_id, max(started_at) as marker_at
    from {{ dbt_dqm.dqm_relation('dqm_reconciliation_runs') }}
    where status = 'completed'
    group by run_id
  ) markers
  group by invocation_id
),
-- The baseline is the latest successful invocation by marker; with no success, the earliest
-- attributed outcome. Earlier triggers are irrelevant to the step's current state.
baselines as (
  select expected.step,
    max(case when io.outcome = 'succeeded' then io.order_at end) as success_at,
    min(io.order_at) as first_at
  from expected
  left join invocation_outcomes io on io.step = expected.step
  group by expected.step
),
thresholds as (
  select step, coalesce(success_at, first_at) as threshold_at from baselines
),
missing as (
  select th.step, count(*) as n
  from thresholds th
  cross join triggers t
  left join invocation_outcomes io on io.step = th.step and io.invocation_id = t.invocation_id
  where t.marker_at >= th.threshold_at and io.invocation_id is null
  group by th.step
),
unresolved as (
  select th.step,
    sum(case when io.outcome = 'failed' and io.order_at >= th.threshold_at then 1 else 0 end) as failed_n,
    sum(case when io.outcome = 'ambiguous' and (io.order_at is null or io.order_at >= th.threshold_at) then 1 else 0 end) as ambiguous_n
  from thresholds th
  inner join invocation_outcomes io on io.step = th.step
  group by th.step
),
-- Unattributed outcomes stay relevant until a later attributed outcome for the step exists.
pending as (
  select u.step, count(*) as n
  from invocation_outcomes u
  where u.order_at is null
    and not exists(select 1 from invocation_outcomes a
      where a.step = u.step and a.order_at is not null and a.logged_at > u.logged_at)
  group by u.step
),
display as (
  select expected.step,
    max(case when o.outcome = 'succeeded' then o.logged_at end) as last_success_at,
    max(case when o.outcome = 'failed' then o.logged_at end) as last_failure_at
  from expected
  left join outcome_rows o on o.step = expected.step
  group by expected.step
),
latest_failure as (
  select step, diagnostic_id from (
    select step, diagnostic_id,
      row_number() over (partition by step order by logged_at desc) as recency
    from outcome_rows where outcome = 'failed'
  ) ranked where recency = 1
),
activity as (
  select max(marker_at) as last_dqm_activity_at from triggers
)
select expected.step, display.last_success_at, display.last_failure_at,
  latest_failure.diagnostic_id as last_failure_diagnostic_id, activity.last_dqm_activity_at,
  case
    when th.threshold_at is null then 'unknown'
    when coalesce(missing.n, 0) > 0 or coalesce(unresolved.ambiguous_n, 0) > 0 or coalesce(pending.n, 0) > 0 then 'unknown'
    when coalesce(unresolved.failed_n, 0) > 0 then 'failing'
    else 'ok'
  end as state
from expected
inner join thresholds th on th.step = expected.step
inner join display on display.step = expected.step
left join missing on missing.step = expected.step
left join unresolved on unresolved.step = expected.step
left join pending on pending.step = expected.step
left join latest_failure on latest_failure.step = expected.step
cross join activity
