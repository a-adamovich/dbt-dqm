{% macro issues_with_history(active_only=false) %}
select cur.*, prev.occurrence_id is not null as has_previous_occurrence,
  coalesce(prev.close_reason='Passed',false) as is_recurrence,
  coalesce(prev.close_reason='Passed' and prev.workflow_status_at_close='RESOLVED',false) as is_potential_regression,
  prev.occurrence_id as previous_occurrence_id, prev.archived_at as previous_archived_at,
  prev.close_reason as previous_close_reason, prev.workflow_status_at_close as previous_workflow_status_at_close,
  prev.review_verdict as previous_review_verdict, prev.poc_responsible as previous_poc_responsible,
  prev.ticket_url as previous_ticket_url, prev.notes as previous_notes
from {{ dbt_dqm.dqm_relation('dqm_issue_occurrences') }} cur
left join {{ dbt_dqm.dqm_relation('dqm_issue_occurrences') }} prev
  on cur.test_unique_id=prev.test_unique_id and cur.unique_id=prev.unique_id
  and cur.identity_scheme_signature is not distinct from prev.identity_scheme_signature
  and cur.occurrence_number=prev.occurrence_number+1
where {% if active_only %}cur.record_status='Active'{% else %}true{% endif %}
{% endmacro %}
