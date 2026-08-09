-- Demonstrates a three-column composite identity with five descriptive attributes.
-- It exercises mixed numeric and textual context on the widest demo failure output.
{{
  config(
    tags=['dqm', 'Demo', 'Operations'],
    store_failures=true,
    severity='warn',
    meta={
      'dbt_dqm': {
        'granularity': ['customer_id', 'order_id', 'line_id'],
        'poc_responsible': 'order_operations_owner',
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
