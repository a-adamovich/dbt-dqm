-- depends_on: {{ ref('dqm_reconcile') }}
select event_id,count(*) as duplicate_count
from {{ dbt_dqm.dqm_relation('dqm_issue_events') }}
group by event_id having count(*)>1
