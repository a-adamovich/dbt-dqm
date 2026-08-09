-- Demonstrates a single-column identity with two descriptive attributes.
-- Case-only customer_id duplicates collapse into one observation while display casing is retained.
{{
  config(
    tags=['dqm', 'Demo', 'Customer'],
    store_failures=true,
    severity='warn',
    meta={
      'dbt_dqm': {
        'granularity': ['customer_id'],
        'poc_responsible': 'customer_data_owner',
        'call_to_action': 'Correct the customer email address'
      }
    }
  )
}}

select customer_id, email, reason
from {{ ref('demo_records') }}
where issue_family = 'customer_email'
