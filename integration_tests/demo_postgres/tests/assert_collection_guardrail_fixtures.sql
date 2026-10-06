with fixture_outcomes as (
  select test_name, count(*) as configuration_errors
  from {{ dbt_dqm.relation_name('dqm_test_executions') }}
  where test_name in (
    'demo_invalid_empty_grain',
    'demo_invalid_duplicate_grain',
    'demo_invalid_non_string_grain',
    'demo_invalid_missing_column'
  )
    and collection_status = 'configuration_error'
  group by test_name
),
expected as (
  select 'demo_invalid_empty_grain' as test_name
  union all select 'demo_invalid_duplicate_grain'
  union all select 'demo_invalid_non_string_grain'
  union all select 'demo_invalid_missing_column'
)
select expected.test_name
from expected
left join fixture_outcomes using (test_name)
where coalesce(fixture_outcomes.configuration_errors, 0) = 0
