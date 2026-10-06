{{ config(tags=['dqm','guardrail'],store_failures=true,severity='warn',store_failures_as='view',meta={'dbt_dqm':{'granularity': ['customer_id']}}) }}
select customer_id from {{ ref('demo_records') }} where false
