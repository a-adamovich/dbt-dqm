-- depends_on: {{ ref('dqm_reconcile') }}
{{ config(enabled=var('dqm_enabled',false), schema=var('dbt_dqm_schema', none), materialized='view') }}
{{ dbt_dqm.issues_with_history(true) }}
