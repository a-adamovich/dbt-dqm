{{ config(materialized='view') }}
select * from {{ dbt_dqm.dqm_relation('dqm_issue_occurrences') }}
{% if dbt_dqm.empty_mode() %}where false{% endif %}
