{{ config(tags=['dqm','guardrail'],store_failures=true,severity='warn',meta={'dbt_dqm':{'granularity': ['customer_id'], 'priority': 'urgent'}}) }}
select customer_id from {{ ref('demo_records') }} where false
