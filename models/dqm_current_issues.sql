-- depends_on: {{ ref('dqm_reconcile') }}
{{ config(schema=var('dbt_dqm_schema', none), materialized='view') }}
{{ dbt_dqm.issues_with_history(true) }}
