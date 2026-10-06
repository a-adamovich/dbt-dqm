with fixture_outcomes as (
  select test_name, count(*) as configuration_errors
  from {{ dbt_dqm.relation_name('dqm_test_executions') }}
  where test_name in (
    'demo_invalid_empty_grain',
    'demo_invalid_duplicate_grain',
    'demo_invalid_non_string_grain',
    'demo_invalid_missing_column',
    'demo_invalid_fail_calc',
    'demo_invalid_limit',
    'demo_invalid_view_storage',
    'demo_invalid_owner',
    'demo_invalid_priority'
  )
    and collection_status = 'configuration_error'
  group by test_name
),
expected as (
  select 'demo_invalid_empty_grain' as test_name
  union all select 'demo_invalid_duplicate_grain'
  union all select 'demo_invalid_non_string_grain'
  union all select 'demo_invalid_missing_column'
  union all select 'demo_invalid_fail_calc'
  union all select 'demo_invalid_limit'
  union all select 'demo_invalid_view_storage'
  union all select 'demo_invalid_owner'
  union all select 'demo_invalid_priority'
)
select expected.test_name
from expected
left join fixture_outcomes using (test_name)
where coalesce(fixture_outcomes.configuration_errors, 0) = 0

{% if dbt_dqm.empty_mode() %}and false{% endif %}
