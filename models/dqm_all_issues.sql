-- depends_on: {{ ref('dqm_reconcile') }}
{{ config(materialized='view') }}
{{ dbt_dqm.issues_with_history() }}
