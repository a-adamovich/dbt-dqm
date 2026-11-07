{{ config(enabled=var('demo_generic_checks', false), tags=['dqm_generic_checks'],
    store_failures=true, severity='warn', limit=100,
    meta={'dbt_dqm': {'granularity': ['id']}}) }}

select * from {{ ref('generic_check_records') }} where status = 'invalid'
