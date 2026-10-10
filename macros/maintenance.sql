{#
  Optional maintenance (#13): raw-log pruning, event retention, BigQuery staging expiry and stage
  drops, run-ledger pruning and maintenance-log pruning.

  None of it may fail an otherwise successful dbt invocation. Each step runs in its own protected
  block, rolls back its own transaction on error, and records its outcome in
  dqm_maintenance_log through a separately protected insert. A step that fails is retried the
  next time maintenance runs. Capture, reconciliation, migrations and setup are not wrapped:
  their failures stay visible.

  The log never stores warehouse error text, which can contain captured values. A row holds a
  fixed description for the step and outcome plus a safe diagnostic identifier: the BigQuery
  script job ID (look it up in job history) or the Postgres SQLSTATE code.

  Each step's SQL comes from a dispatched macro, so acceptance projects can inject faults into a
  single step without test switches in the package.
#}

{% macro maintenance_steps(days, events_days) %}
  {% set steps = [] %}
  {% if days is not none %}{% do steps.append({'name': 'raw_pruning', 'transactional': true}) %}{% endif %}
  {% if events_days is not none %}{% do steps.append({'name': 'event_retention', 'transactional': false}) %}{% endif %}
  {% if target.type == 'bigquery' %}
    {% do steps.append({'name': 'staging_expiry', 'transactional': false}) %}
    {% do steps.append({'name': 'stage_drops', 'transactional': false}) %}
  {% endif %}
  {% if days is not none %}{% do steps.append({'name': 'run_pruning', 'transactional': true}) %}{% endif %}
  {#- The log only needs pruning when some maintenance writes to it. #}
  {% if steps %}{% do steps.append({'name': 'log_pruning', 'transactional': false}) %}{% endif %}
  {{ return(steps) }}
{% endmacro %}

{# --- Step SQL (dispatched; acceptance projects may override one step to inject a fault) --- #}

{% macro maintenance_raw_pruning(days) %}{{ return(adapter.dispatch('maintenance_raw_pruning', 'dbt_dqm')(days)) }}{% endmacro %}
{% macro default__maintenance_raw_pruning(days) %}
  {% set cutoff = dbt_dqm.timestamp_add('day', -days, dbt.current_timestamp()) %}
  update {{ dbt_dqm.dqm_relation('dqm_reconciliation_control') }}
    set raw_pruned_before=case when raw_pruned_before > {{ cutoff }} then raw_pruned_before else {{ cutoff }} end,
      generation=generation+1
    where true;
  {% set eligible %}
    select execution.test_unique_id,execution.invocation_id
    from {{ dbt_dqm.dqm_relation('dqm_test_executions') }} execution
    inner join {{ dbt_dqm.dqm_relation('dqm_reconciliation_receipts') }} receipt using(test_unique_id,invocation_id)
    where execution.captured_at < {{ cutoff }}
      and not exists(select 1 from {{ dbt_dqm.dqm_relation('dqm_reconciliation_inputs') }} input
        inner join {{ dbt_dqm.dqm_relation('dqm_reconciliation_runs') }} run using(run_id)
        where run.status='started' and input.test_unique_id=execution.test_unique_id and input.invocation_id=execution.invocation_id)
  {% endset %}
  {% if target.type == 'postgres' %}
    create temporary table dqm_prune on commit drop as {{ eligible }};
  {% else %}
    create or replace temp table dqm_prune as {{ eligible }};
  {% endif %}
  {% for name in ['dqm_issue_observations', 'dqm_reconciliation_receipts', 'dqm_test_executions'] %}
    delete from {{ dbt_dqm.dqm_relation(name) }} target where exists(select 1 from dqm_prune prune
      where prune.test_unique_id=target.test_unique_id and prune.invocation_id=target.invocation_id);
  {% endfor %}
  {% if target.type == 'postgres' %}drop table dqm_prune;{% endif %}
{% endmacro %}

{% macro maintenance_event_retention(events_days) %}{{ return(adapter.dispatch('maintenance_event_retention', 'dbt_dqm')(events_days)) }}{% endmacro %}
{% macro default__maintenance_event_retention(events_days) %}
  delete from {{ dbt_dqm.dqm_relation('dqm_issue_events') }} where event_at < {{ dbt_dqm.timestamp_add('day', -events_days, dbt.current_timestamp()) }};
{% endmacro %}

{# Expired app uploads only: rows staged within 24 hours belong to uploads that may still apply. #}
{% macro maintenance_staging_expiry() %}{{ return(adapter.dispatch('maintenance_staging_expiry', 'dbt_dqm')()) }}{% endmacro %}
{% macro default__maintenance_staging_expiry() %}
  delete from {{ dbt_dqm.dqm_relation('dqm_app_change_staging') }} where staged_at <= timestamp_sub(current_timestamp(), interval 24 hour);
{% endmacro %}

{% macro maintenance_stage_drops() %}{{ return(adapter.dispatch('maintenance_stage_drops', 'dbt_dqm')()) }}{% endmacro %}
{% macro default__maintenance_stage_drops() %}{{ dbt_dqm.cleanup_stages_sql() }}{% endmacro %}

{% macro maintenance_run_pruning(days) %}{{ return(adapter.dispatch('maintenance_run_pruning', 'dbt_dqm')(days)) }}{% endmacro %}
{% macro default__maintenance_run_pruning(days) %}
  {% set cutoff = dbt_dqm.timestamp_add('day', -days, dbt.current_timestamp()) %}
  delete from {{ dbt_dqm.dqm_relation('dqm_reconciliation_inputs') }} input where exists(select 1 from {{ dbt_dqm.dqm_relation('dqm_reconciliation_runs') }} run
    where run.run_id=input.run_id and run.status!='started' and run.completed_at < {{ cutoff }});
  delete from {{ dbt_dqm.dqm_relation('dqm_reconciliation_runs') }} where status!='started' and completed_at < {{ cutoff }};
{% endmacro %}

{# Only rows older than 30 days, so this step can never remove an outcome just written. #}
{% macro maintenance_log_pruning() %}{{ return(adapter.dispatch('maintenance_log_pruning', 'dbt_dqm')()) }}{% endmacro %}
{% macro default__maintenance_log_pruning() %}
  delete from {{ dbt_dqm.dqm_relation('dqm_maintenance_log') }} where logged_at < {{ dbt_dqm.timestamp_add('day', -30, dbt.current_timestamp()) }};
{% endmacro %}

{% macro maintenance_step_sql(step, days, events_days) %}
  {% if step.name == 'raw_pruning' %}{{ return(dbt_dqm.maintenance_raw_pruning(days)) }}
  {% elif step.name == 'event_retention' %}{{ return(dbt_dqm.maintenance_event_retention(events_days)) }}
  {% elif step.name == 'staging_expiry' %}{{ return(dbt_dqm.maintenance_staging_expiry()) }}
  {% elif step.name == 'stage_drops' %}{{ return(dbt_dqm.maintenance_stage_drops()) }}
  {% elif step.name == 'run_pruning' %}{{ return(dbt_dqm.maintenance_run_pruning(days)) }}
  {% else %}{{ return(dbt_dqm.maintenance_log_pruning()) }}{% endif %}
{% endmacro %}

{# --- Outcome logging: fixed descriptions, safe identifiers only --- #}

{% macro maintenance_log_insert_sql(step_name, ok_expression, diagnostic_expression) %}
  {{ return(adapter.dispatch('maintenance_log_insert_sql', 'dbt_dqm')(step_name, ok_expression, diagnostic_expression)) }}
{% endmacro %}
{% macro default__maintenance_log_insert_sql(step_name, ok_expression, diagnostic_expression) %}
  insert into {{ dbt_dqm.dqm_relation('dqm_maintenance_log') }}
    (logged_at,invocation_id,step,outcome,description,diagnostic_id)
  values ({{ dbt.current_timestamp() }}, {{ dbt_dqm.sql_string(invocation_id) }}, {{ dbt_dqm.sql_string(step_name) }},
    case when {{ ok_expression }} then 'succeeded' else 'failed' end,
    case when {{ ok_expression }} then {{ dbt_dqm.sql_string(step_name | replace('_', ' ') ~ ' succeeded') }}
      else {{ dbt_dqm.sql_string(step_name | replace('_', ' ') ~ ' failed') }} end,
    case when {{ ok_expression }} then cast(null as {{ dbt.type_string() }}) else {{ diagnostic_expression }} end);
{% endmacro %}

{# --- The protected maintenance script --- #}

{% macro maintenance_sql(days, events_days) %}
  {% set steps = dbt_dqm.maintenance_steps(days, events_days) %}
  {% if target.type == 'bigquery' %}
    {{ dbt_dqm.bigquery__maintenance_script(steps, days, events_days) }}
  {% else %}
    {{ dbt_dqm.postgres__maintenance_script(steps, days, events_days) }}
  {% endif %}
{% endmacro %}

{% macro bigquery__maintenance_script(steps, days, events_days) %}
  declare dqm_step_ok bool default false;
  declare dqm_step_txn bool default false;
  declare dqm_step_job string default @@script.job_id;
  {% for step in steps %}
    set dqm_step_ok = false;
    set dqm_step_txn = false;
    begin
      {% if step.transactional %}begin transaction; set dqm_step_txn = true;{% endif %}
      {{ dbt_dqm.ready_sql() }}
      {{ dbt_dqm.maintenance_step_sql(step, days, events_days) }}
      {% if step.transactional %}set dqm_step_txn = false; commit transaction;{% endif %}
      set dqm_step_ok = true;
    exception when error then
      {#- Close this step's transaction before anything else runs. BigQuery raises an error that
          no exception handler can catch when ROLLBACK runs without an open transaction, so roll
          back only while this step's transaction is known to be open. The flag is cleared just
          before COMMIT: a failed commit leaves nothing to roll back here, and a transaction
          still open when the script ends is discarded by BigQuery without failing the job. #}
      if dqm_step_txn then
        rollback transaction;
      end if;
    end;
    begin
      {{ dbt_dqm.maintenance_log_insert_sql(step.name, 'dqm_step_ok', 'dqm_step_job') }}
    exception when error then
      select 'maintenance outcome not logged' as maintenance_note;
    end;
  {% endfor %}
{% endmacro %}

{% macro postgres__maintenance_script(steps, days, events_days) %}
  {#- The outer transaction holds the advisory lock (serialized with reconciliation and
      migrations). Each step is a PL/pgSQL block whose EXCEPTION clause rolls back to its own
      savepoint, so a failed step leaves no partial changes and the next step still runs. #}
  begin;
  {{ dbt_dqm.reconcile_lock() }}
  {% for step in steps %}
    do $dqm_maintenance$
    declare
      dqm_step_ok boolean := false;
      dqm_step_state text := null;
    begin
      begin
        {{ dbt_dqm.ready_sql() }}
        {{ dbt_dqm.maintenance_step_sql(step, days, events_days) }}
        dqm_step_ok := true;
      exception when others then
        get stacked diagnostics dqm_step_state = returned_sqlstate;
      end;
      begin
        {{ dbt_dqm.maintenance_log_insert_sql(step.name, 'dqm_step_ok', 'dqm_step_state') }}
      exception when others then
        null;
      end;
    end $dqm_maintenance$;
  {% endfor %}
  commit;
{% endmacro %}
