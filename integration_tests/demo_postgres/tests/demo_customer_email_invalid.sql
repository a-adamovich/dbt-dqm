-- Demonstrates a single-column identity with two descriptive attributes.
-- Case-only customer_id duplicates collapse into one observation while display casing is retained.
{{
  config(
    tags=['dqm', 'Demo', 'Customer'],
    store_failures=true,
    severity='warn',
    meta={
      'dbt_dqm': {
        'granularity': var('demo_grain', ['customer_id']),
        'priority': var('demo_priority', 'high'),
        'criticality': 'customer contact',
        'capture_mode': 'allowlist',
        'context_columns': ['email', 'reason'],
        'poc_responsible': 'customer_data_owner',
        'call_to_action': 'Correct the customer email address'
      }
    }
  )
}}

select customer_id, email, reason,
  case when lower(customer_id) = 'cust-001' then {% if var('demo_owner_conflict',false) %}case when customer_id='CUST-001' then 'sales' else 'support' end{% else %}'customer_success'{% endif %} else cast(null as {{ dbt.type_string() }}) end as DQM_OWNER
from {{ ref('demo_records') }}
where issue_family = 'customer_email'
