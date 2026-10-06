{{ config(tags=['dqm','guardrail'],store_failures=true,severity='warn',meta={'dbt_dqm':{'granularity': ['customer_id'], 'owner_column': 'missing_owner'}}) }}
select customer_id from {{ ref('demo_records') }} where false
