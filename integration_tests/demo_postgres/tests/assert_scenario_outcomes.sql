-- Scripted lifecycle assertions for the demo walkthrough documented in README.md, replacing what
-- was previously a manual, human-verified visual check. Assumes the documented, reset-then-ordered
-- sequence: `dbt run-operation reset_demo_dqm`, then initial -> passed -> recurrence, running
-- `dbt test --select tag:dqm && dbt build --select package:dbt_dqm` between each `demo_scenario`
-- change. Run this after each step with the same `--vars '{demo_scenario: ...}'` used for that
-- step: `dbt test --select assert_scenario_outcomes --vars '{demo_scenario: initial}'`. Any
-- returned row is a mismatch between the documented expected outcome and the actual state of
-- dqm_all_issues.
{% set scenario = var('demo_scenario', 'initial') %}

with actual as (
  select
    test_name,
    record_status,
    count(*) as occurrence_count,
    max(occurrence_number) as max_occurrence_number
  from {{ ref('dqm_all_issues') }}
  where test_name in (
    'demo_customer_email_invalid', 'demo_order_amount_invalid', 'demo_order_line_invalid'
  )
  group by 1, 2
),

{% if scenario == 'initial' %}
-- Case-only key duplicates collapse: 2 active identities for the 1-column grain test, 2 for the
-- 2-column grain test, 3 for the 3-column grain test. Nothing has been archived yet.
expected as (
  select 'demo_customer_email_invalid' as test_name, 'Active' as record_status, 2 as occurrence_count, 1 as max_occurrence_number
  union all select 'demo_order_amount_invalid', 'Active', 2, 1
  union all select 'demo_order_line_invalid', 'Active', 3, 1
),
{% elif scenario == 'passed' %}
-- No seed rows match this scenario, so every tracked test reports zero failures: every prior
-- occurrence is archived as a genuine Passed (granularity is unchanged).
expected as (
  select 'demo_customer_email_invalid' as test_name, 'Archived' as record_status, 2 as occurrence_count, 1 as max_occurrence_number
  union all select 'demo_order_amount_invalid', 'Archived', 2, 1
  union all select 'demo_order_line_invalid', 'Archived', 3, 1
),
{% elif scenario == 'recurrence' %}
-- One recurring identity per test, run after 'passed' archived everything: each gets a fresh
-- occurrence_number 2 with blank annotations, alongside the untouched Archived history.
expected as (
  select 'demo_customer_email_invalid' as test_name, 'Active' as record_status, 1 as occurrence_count, 2 as max_occurrence_number
  union all select 'demo_order_amount_invalid', 'Active', 1, 2
  union all select 'demo_order_line_invalid', 'Active', 1, 2
  union all select 'demo_customer_email_invalid', 'Archived', 2, 1
  union all select 'demo_order_amount_invalid', 'Archived', 2, 1
  union all select 'demo_order_line_invalid', 'Archived', 3, 1
),
{% else %}
-- Unrecognized demo_scenario: nothing to assert against, so nothing can mismatch.
expected as (
  select
    cast(null as {{ dbt.type_string() }}) as test_name,
    cast(null as {{ dbt.type_string() }}) as record_status,
    cast(null as {{ dbt.type_int() }}) as occurrence_count,
    cast(null as {{ dbt.type_int() }}) as max_occurrence_number
  from {{ dbt_dqm.dual() }} where false
),
{% endif %}

mismatches as (
  select
    coalesce(expected.test_name, actual.test_name) as test_name,
    coalesce(expected.record_status, actual.record_status) as record_status,
    expected.occurrence_count as expected_occurrence_count,
    actual.occurrence_count as actual_occurrence_count,
    expected.max_occurrence_number as expected_max_occurrence_number,
    actual.max_occurrence_number as actual_max_occurrence_number
  from expected
  full outer join actual
    on expected.test_name = actual.test_name
   and expected.record_status = actual.record_status
  where expected.occurrence_count is distinct from actual.occurrence_count
     or expected.max_occurrence_number is distinct from actual.max_occurrence_number
)

select * from mismatches {% if dbt_dqm.empty_mode() %}where false{% endif %}
