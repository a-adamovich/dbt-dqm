-- Demonstrates a two-column composite identity with three descriptive attributes.
-- The case-insensitive grain is customer_id plus order_id; amount, currency, and reason are context.
{{
  config(
    tags=['dqm', 'Demo', 'Finance'],
    store_failures=true, enabled=var('dqm_enabled',false), limit=var('dbt_dqm_default_test_limit',100),
    severity='warn',
    meta={
      'dbt_dqm': {
        'granularity': ['customer_id', 'order_id'],
        'owner': 'finance_data_owner',
        'call_to_action': 'Investigate the invalid settled amount'
      }
    }
  )
}}

select customer_id, order_id, amount, currency, reason,
  cast(null as {{ dbt.type_string() }}) as dqm_owner
from {{ ref('demo_records') }}
where issue_family = 'order_amount'
