-- depends_on: {{ ref('dqm_missed_issues') }}
{#- Postgres replaces this view in place inside the locked reconcile transaction (see
    macros/materializations.sql, #17); BigQuery keeps its adapter's view materialization. #}
{{ config(enabled=var('dqm_enabled',false), schema=var('dbt_dqm_schema', none), materialized=('dqm_entry_view' if target.type == 'postgres' else 'view')) }}
select * from {{ dbt_dqm.dqm_relation('dqm_issue_occurrences') }}
