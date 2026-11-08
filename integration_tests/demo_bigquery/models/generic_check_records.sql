{{ config(enabled=var('demo_generic_checks', false)) }}

select 'generic-1' as id, 'invalid' as status
from (select 1)
where {{ dbt.string_literal(var('demo_generic_scenario', 'initial')) }} != 'passed'
