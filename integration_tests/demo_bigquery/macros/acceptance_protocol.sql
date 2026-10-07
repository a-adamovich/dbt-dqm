{# Synthetic acceptance operations only; not shipped as package macros. #}
{% macro acceptance_freeze() %}
  {% if execute %}{% do run_query(dbt_dqm.reconcile_pre()) %}{% endif %}
{% endmacro %}
{% macro acceptance_apply(run_id,fault=false) %}
  {% if not modules.re.fullmatch('[0-9a-f-]{36}',run_id) %}{{ exceptions.raise_compiler_error('Expected a test run UUID.') }}{% endif %}
  {% set sql=dbt_dqm.apply_reconciliation()|replace(invocation_id,run_id)|replace(invocation_id|replace('-',''),run_id|replace('-','')) %}
  {% if fault %}
    {% set sql=sql|replace('merge ' ~ dbt_dqm.dqm_relation('dqm_reconciliation_state'), "select error('injected apply failure'); merge " ~ dbt_dqm.dqm_relation('dqm_reconciliation_state')) %}
  {% endif %}
  {% if execute %}{% do run_query(sql) %}{% endif %}
{% endmacro %}

{% macro acceptance_setup() %}{% if execute %}{% do run_query(dbt_dqm.setup_sql()) %}{% endif %}{% endmacro %}
{% macro bigquery__migrations() %}
  {% set registry=dbt_dqm.default__migrations() %}
  {% if var('acceptance_future',false) %}{% do registry.append({'id':'9999_acceptance','apply':'acceptance_add','backfill':'acceptance_backfill','verify':'acceptance_verify'}) %}{% endif %}
  {{ return(registry) }}
{% endmacro %}
{% macro default__acceptance_add() %}
  alter table {{ dbt_dqm.dqm_relation('dqm_issue_occurrences') }} add column if not exists acceptance_future_field string;
  {% if var('acceptance_interrupt',false) %}select error('injected migration interruption');{% endif %}
{% endmacro %}
{% macro default__acceptance_backfill() %}
  update {{ dbt_dqm.dqm_relation('dqm_issue_occurrences') }} set acceptance_future_field='preserved' where acceptance_future_field is null;
{% endmacro %}
{% macro default__acceptance_verify() %}
  {{ dbt_dqm.assert_sql('not exists(select 1 from ' ~ dbt_dqm.dqm_relation('dqm_issue_occurrences') ~ ' where acceptance_future_field is null)',"'incomplete backfill'") }}
{% endmacro %}
