{{ config(tags=['dqm','guardrail'],store_failures=true,severity='warn',limit=1,meta={'dbt_dqm':{'granularity': ['customer_id']}}) }}
select customer_id from {{ ref('demo_records') }} where false
