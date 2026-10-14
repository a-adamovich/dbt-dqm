{{ config(tags=['dqm','guardrail'],store_failures=true, enabled=var('dqm_enabled',false) and var('demo_invalid_configurations',false), severity='warn',limit=0,meta={'dbt_dqm':{'granularity': ['customer_id']}}) }}
select customer_id from {{ ref('demo_records') }} where false
