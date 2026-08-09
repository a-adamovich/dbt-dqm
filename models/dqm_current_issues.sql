-- Reviewer-facing projection containing only currently active issue occurrences.
-- Lifecycle history remains available through dqm_all_issues.
{{ config(materialized='view') }}

select *
from {{ ref('dqm_reconcile') }}
where record_status = 'Active'
