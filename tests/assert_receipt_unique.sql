-- depends_on: {{ ref('dqm_reconcile') }}
select test_unique_id,invocation_id,count(*) as duplicate_count
from {{ dbt_dqm.dqm_relation('dqm_reconciliation_receipts') }}
group by test_unique_id,invocation_id having count(*)>1
