{{ config(tags=['dqm','guardrail'],store_failures=true, enabled=var('dqm_enabled',false) and var('demo_invalid_configurations',false), limit=var('dbt_dqm_default_test_limit',100),severity='warn',meta={'dbt_dqm':{'granularity': ['customer_id'], 'priority': 'urgent'}}) }}
select customer_id from {{ ref('demo_records') }} where false
