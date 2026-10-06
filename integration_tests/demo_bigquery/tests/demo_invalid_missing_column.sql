{{
  config(
    tags=['dqm', 'dqm_guardrail_fixture'],
    store_failures=true,
    severity='warn',
    meta={'dbt_dqm': {'granularity': ['missing_identity_column']}}
  )
}}

select customer_id from {{ ref('demo_records') }}
