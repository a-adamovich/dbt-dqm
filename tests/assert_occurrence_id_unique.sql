-- depends_on: {{ ref('dqm_reconcile') }}
select occurrence_id,count(*) as duplicate_count
from {{ dbt_dqm.dqm_relation('dqm_issue_occurrences') }}
group by occurrence_id having count(*)>1
