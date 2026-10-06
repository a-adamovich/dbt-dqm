{{ config(tags=['dqm','guardrail'],store_failures=true,severity='warn',fail_calc='coalesce(sum(1),0)',meta={'dbt_dqm':{'granularity': ['customer_id']}}) }}
select customer_id from {{ ref('demo_records') }} where false
