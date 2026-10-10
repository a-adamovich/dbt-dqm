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

{# Lease takeover: an owner that claimed setup and applied DDL, then stalled. #}
{% macro acceptance_future_spec() %}
  {% for spec in dbt_dqm.migrations() if spec.id == '9999_acceptance' %}{{ return(spec) }}{% endfor %}
  {{ exceptions.raise_compiler_error('Run with acceptance_future: true') }}
{% endmacro %}
{% macro acceptance_stalled_owner(token) %}
  {% set control = dbt_dqm.dqm_relation('dqm_reconciliation_control') %}
  {% set sql %}
    begin transaction;
    update {{ control }} set setup_status='migrating', migration_owner={{ dbt_dqm.sql_string(token) }},
      migration_lease_until=timestamp_add(current_timestamp(), interval 60 minute), generation=generation+1 where true;
    commit transaction;
    {{ dbt_dqm.bigquery_migration_apply(acceptance_future_spec(), dbt_dqm.sql_string(token)) }}
  {% endset %}
  {% if execute %}{% do run_query(sql) %}{% endif %}
{% endmacro %}
{% macro acceptance_resume_commit(token) %}
  {% if execute %}{% do run_query(dbt_dqm.bigquery_migration_commit(acceptance_future_spec(), dbt_dqm.sql_string(token))) %}{% endif %}
{% endmacro %}
{% macro acceptance_resume_release(token) %}
  {% if execute %}{% do run_query(dbt_dqm.bigquery_migration_release(dbt_dqm.sql_string(token))) %}{% endif %}
{% endmacro %}
{% macro bigquery__event_payload_mode_backfill() %}
  {{ dbt_dqm.default__event_payload_mode_backfill() }}
  {% if var('interrupt_0002', false) %}select error('injected 0002 interruption');{% endif %}
{% endmacro %}

{% macro bigquery__app_staging_safety_backfill() %}
  {{ dbt_dqm.default__app_staging_safety_backfill() }}
  {% if var('interrupt_0004', false) %}select error('injected 0004 interruption');{% endif %}
{% endmacro %}
{# Optional-maintenance fault injection (#13). #}
{% macro bigquery__maintenance_staging_expiry() %}
  {{ dbt_dqm.default__maintenance_staging_expiry() }}
  {% if var('fail_step', '') == 'staging_expiry' %}select error('{{ var("fail_marker", "injected") }}');{% endif %}
{% endmacro %}
{% macro bigquery__maintenance_raw_pruning(days) %}
  {{ dbt_dqm.default__maintenance_raw_pruning(days) }}
  {% if var('fail_step', '') == 'raw_pruning' %}select error('{{ var("fail_marker", "injected") }}');{% endif %}
  {#- Simulates the state after a failed COMMIT: the transaction is still open but the step's
      flag says there's nothing to roll back. The open transaction must end with the step job. #}
  {% if var('fail_step', '') == 'raw_pruning_open_transaction' %}set dqm_step_txn = false; select error('injected open transaction');{% endif %}
{% endmacro %}
{#- Fails the inner drop of one existing stage, chosen by a fragment of its relation name. #}
{% macro bigquery__maintenance_stage_drop_sql(stage_relation) %}
  {% if var('fail_stage_drop', '') %}
    if strpos({{ stage_relation }}, {{ dbt_dqm.sql_string(var('fail_stage_drop')) }}) > 0 then
      select error('injected stage drop failure');
    end if;
  {% endif %}
  {{ dbt_dqm.default__maintenance_stage_drop_sql(stage_relation) }}
{% endmacro %}
{% macro bigquery__maintenance_log_pruning() %}
  {{ dbt_dqm.default__maintenance_log_pruning() }}
  {% if var('fail_step', '') == 'log_pruning' %}select error('{{ var("fail_marker", "injected") }}');{% endif %}
{% endmacro %}
{% macro bigquery__maintenance_log_insert_sql(step_name, ok_expression, diagnostic_expression, marker, kind) %}
  {% if var('fail_log', false) %}select error('maintenance log unavailable');
  {% else %}{{ dbt_dqm.default__maintenance_log_insert_sql(step_name, ok_expression, diagnostic_expression, marker, kind) }}{% endif %}
{% endmacro %}
