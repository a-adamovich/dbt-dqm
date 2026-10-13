-- Demonstrates a three-column composite identity with five descriptive attributes.
-- It exercises mixed numeric and textual context on the widest demo failure output.
{{
  config(
    tags=['dqm', 'Demo', 'Operations'],
    store_failures=true, enabled=var('dqm_enabled',false), limit=var('dbt_dqm_default_test_limit',100),
    severity='warn',
    meta={
      'dbt_dqm': {
        'granularity': ['customer_id', 'order_id', 'line_id'],
        'owner': 'order_operations_owner',
        'call_to_action': 'Repair the invalid order-line values'
      }
    }
  )
}}

select
  customer_id,
  order_id,
  line_id,
  sku,
  quantity,
  unit_price,
  reason,
  source_system
from {{ ref('demo_records') }}
where issue_family = 'order_line'
