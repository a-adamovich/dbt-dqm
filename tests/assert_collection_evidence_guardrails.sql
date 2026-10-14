{{ config(enabled=var('dqm_enabled',false)) }}
-- Incomplete/configuration/inconclusive executions must never be admitted as lifecycle evidence.
-- depends_on: {{ ref('dqm_reconcile') }}
select *
from {{ dbt_dqm.relation_name('dqm_test_executions') }}
where (
    coalesce(failure_count, 0) > 0
    and failure_relation is null
    and collection_status in ('success', 'not_applicable', 'limit_reached')
  )
  or (
    lower(test_status) not in ('pass', 'warn', 'fail')
    and collection_status in ('success', 'not_applicable', 'limit_reached')
  )
