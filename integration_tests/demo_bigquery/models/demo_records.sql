-- Scenario-filtered synthetic source shared by all demo tests.
-- Explicit casts keep test identity and attribute types stable across seed inference changes.
select
  cast(issue_family as string) as issue_family,
  cast(customer_id as string) as customer_id,
  cast(order_id as string) as order_id,
  cast(line_id as string) as line_id,
  cast(email as string) as email,
  cast(amount as numeric) as amount,
  cast(currency as string) as currency,
  cast(sku as string) as sku,
  cast(quantity as int64) as quantity,
  cast(unit_price as numeric) as unit_price,
  cast(reason as string) as reason,
  cast(source_system as string) as source_system
from {{ ref('demo_cases') }}
where scenario = {{ dbt.string_literal(var('demo_scenario', 'initial')) }}
