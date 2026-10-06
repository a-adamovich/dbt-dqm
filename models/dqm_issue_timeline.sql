-- depends_on: {{ ref('dqm_reconcile') }}
{{ config(materialized='view') }}
select occurrence_id,test_unique_id,unique_id,identity_scheme_signature,occurrence_number,
  first_seen_at as valid_from,archived_at as valid_to,close_reason,
  record_status='Active' as is_current,record_values_json
from {{ dbt_dqm.dqm_relation('dqm_issue_occurrences') }}
{% if dbt_dqm.empty_mode() %}where false{% endif %}
