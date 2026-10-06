-- Demonstrates a two-column composite identity with three descriptive attributes.
-- The case-insensitive grain is customer_id plus order_id; amount, currency, and reason are context.
{{
  config(
    tags=['dqm', 'Demo', 'Finance'],
    store_failures=true,
    severity='warn',
    meta={
      'dbt_dqm': {
        'granularity': ['customer_id', 'order_id'],
        'capture_mode': 'allowlist',
        'context_columns': ['amount', 'currency', 'reason'],
        'poc_responsible': 'finance_data_owner',
        'call_to_action': 'Investigate the invalid settled amount'
      }
    }
  )
}}

select customer_id, order_id, amount, currency, reason
from {{ ref('demo_records') }}
where issue_family = 'order_amount'
