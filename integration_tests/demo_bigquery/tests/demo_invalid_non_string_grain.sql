{{
  config(
    tags=['dqm', 'dqm_guardrail_fixture'],
    store_failures=true,
    severity='warn',
    meta={'dbt_dqm': {'granularity': ['customer_id', 42]}}
  )
}}

select customer_id from {{ ref('demo_records') }} where false
