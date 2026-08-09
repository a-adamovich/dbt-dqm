-- Scenario-filtered synthetic source shared by all demo tests.
-- Explicit casts keep test identity and attribute types stable across seed inference changes.
select
  cast(issue_family as {{ dbt.type_string() }}) as issue_family,
  cast(customer_id as {{ dbt.type_string() }}) as customer_id,
  cast(order_id as {{ dbt.type_string() }}) as order_id,
  cast(line_id as {{ dbt.type_string() }}) as line_id,
  cast(email as {{ dbt.type_string() }}) as email,
  cast(amount as {{ dbt.type_numeric() }}) as amount,
  cast(currency as {{ dbt.type_string() }}) as currency,
  cast(sku as {{ dbt.type_string() }}) as sku,
  cast(quantity as {{ dbt.type_int() }}) as quantity,
  cast(unit_price as {{ dbt.type_numeric() }}) as unit_price,
  cast(reason as {{ dbt.type_string() }}) as reason,
  cast(source_system as {{ dbt.type_string() }}) as source_system
from {{ ref('demo_cases') }}
where scenario = {{ dbt.string_literal(var('demo_scenario', 'initial')) }}
