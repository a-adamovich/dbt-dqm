{#
  Optional maintenance (#13): raw-log pruning, event retention, BigQuery staging expiry and stage
  drops, run-ledger pruning and maintenance-log pruning.

  Guarantee: a SQL error inside a maintenance step never fails an otherwise successful dbt
  invocation. The failing step rolls back its own work, its outcome is logged, and it's retried
  the next time maintenance runs. This doesn't cover connection loss, cancellation, compilation
  or quota errors, or failing to acquire the Postgres advisory lock. Capture, reconciliation,
  migrations and setup are never wrapped: their failures stay visible.

  Execution (maintenance_run):
    - BigQuery: each step is its own job that ends by selecting its outcome and job ID (the
      first one also reads this invocation's trigger marker, before its own work). After the
      last step, one separate protected job logs every outcome, each row in its own exception
      block. A transaction a step leaves open ends with its own job (BigQuery rolls it back), so
      it can't absorb an outcome row or affect the next step. If a step job fails outside SQL
      (for example, a lost connection), the run stops before logging: the outcomes are missing,
      which Health reports as unknown and explicit cleanup reports as a failure.
    - Postgres: one transaction holds the advisory lock (serialized with reconciliation and
      migrations); each step and each outcome insert is a protected PL/pgSQL subtransaction.

  The log never stores warehouse error text, which can contain captured values. A row holds a
  fixed description, a safe diagnostic ID (the BigQuery step job ID, or the Postgres SQLSTATE),
  and attribution: the invocation's trigger marker, read once before any step runs, and whether
  maintenance ran automatically (after dbt run/build/test) or manually (cleanup_dqm_logs).

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

{#- One stage drop. `stage_relation` is a SQL expression for the stage's relation name. Used by
    both maintenance and the tolerant recovery path (dqm_cleanup_stages). #}
{% macro maintenance_stage_drop_sql(stage_relation) %}{{ return(adapter.dispatch('maintenance_stage_drop_sql', 'dbt_dqm')(stage_relation)) }}{% endmacro %}
{% macro default__maintenance_stage_drop_sql(stage_relation) %}
  execute immediate concat('drop table if exists ', {{ stage_relation }});
{% endmacro %}

{% macro maintenance_stage_drops() %}{{ return(adapter.dispatch('maintenance_stage_drops', 'dbt_dqm')()) }}{% endmacro %}
{% macro default__maintenance_stage_drops() %}{{ dbt_dqm.cleanup_stages_sql(fail_on_error=true) }}{% endmacro %}

{#- BigQuery stage tables live in the DQM dataset as dqm_reconcile_stage_<run_id without dashes>
    (stage_relation in reconciliation.sql). Run pruning keeps every run whose stage still exists,
    so a stage whose drop failed stays retryable. The list is read before the pruning
    transaction; if the lookup fails, the step fails before deleting anything. A stage that
    disappears after the snapshot only keeps its run for one more cycle. #}
{% macro maintenance_live_stages_sql() %}
  create or replace temp table dqm_live_stages as
    select table_name from {{ dbt_dqm.tables_catalog() }}
    where starts_with(table_name, 'dqm_reconcile_stage_');
{% endmacro %}
{% macro maintenance_run_retained_sql(run_alias) %}
  {% if target.type == 'bigquery' %}
    and not exists(select 1 from dqm_live_stages live
      where live.table_name = concat('dqm_reconcile_stage_', replace({{ run_alias }}.run_id, '-', '')))
  {% endif %}
{% endmacro %}

{% macro maintenance_run_pruning(days) %}{{ return(adapter.dispatch('maintenance_run_pruning', 'dbt_dqm')(days)) }}{% endmacro %}
{% macro default__maintenance_run_pruning(days) %}
  {% set cutoff = dbt_dqm.timestamp_add('day', -days, dbt.current_timestamp()) %}
  delete from {{ dbt_dqm.dqm_relation('dqm_reconciliation_inputs') }} input where exists(select 1 from {{ dbt_dqm.dqm_relation('dqm_reconciliation_runs') }} run
    where run.run_id=input.run_id and run.status!='started' and run.completed_at < {{ cutoff }}
    {{ dbt_dqm.maintenance_run_retained_sql('run') }});
  delete from {{ dbt_dqm.dqm_relation('dqm_reconciliation_runs') }} run where run.status!='started' and run.completed_at < {{ cutoff }}
    {{ dbt_dqm.maintenance_run_retained_sql('run') }};
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

{#- Work that must run before a step's transaction opens. #}
{% macro maintenance_step_prelude_sql(step) %}
  {% if target.type == 'bigquery' and step.name == 'run_pruning' %}{{ return(dbt_dqm.maintenance_live_stages_sql()) }}{% endif %}
  {{ return('') }}
{% endmacro %}

{# --- Attribution: the invocation's trigger marker, read once before any step --- #}

{#- The same combined marker dqm_maintenance_health uses: the latest of this invocation's captured
    executions and its completed reconciliation run. #}
{% macro maintenance_trigger_marker_sql(invocation) %}
  (select max(marker_at) from (
    select max(captured_at) as marker_at from {{ dbt_dqm.dqm_relation('dqm_test_executions') }}
      where invocation_id={{ dbt_dqm.sql_string(invocation) }}
    union all
    select max(started_at) as marker_at from {{ dbt_dqm.dqm_relation('dqm_reconciliation_runs') }}
      where run_id={{ dbt_dqm.sql_string(invocation) }} and status='completed'
  ) markers)
{% endmacro %}

{#- BigQuery: reads the marker into dqm_trigger_marker (null when it can't be read). Runs at the
    start of the first step's job, before any step work can prune its source rows. #}
{% macro bigquery__maintenance_marker_sql() %}
  begin
    set dqm_trigger_marker = {{ dbt_dqm.maintenance_trigger_marker_sql(invocation_id) }};
  exception when error then
    set dqm_trigger_marker = null;
  end;
{% endmacro %}

{# --- Outcome logging: fixed descriptions, safe identifiers only --- #}

{#- `marker` is a SQL expression for the trigger marker (null when unknown or manual); `kind` is
    'automatic' or 'manual'. #}
{% macro maintenance_log_insert_sql(step_name, ok_expression, diagnostic_expression, marker, kind) %}
  {{ return(adapter.dispatch('maintenance_log_insert_sql', 'dbt_dqm')(step_name, ok_expression, diagnostic_expression, marker, kind)) }}
{% endmacro %}
{% macro default__maintenance_log_insert_sql(step_name, ok_expression, diagnostic_expression, marker, kind) %}
  insert into {{ dbt_dqm.dqm_relation('dqm_maintenance_log') }}
    (logged_at,invocation_id,step,outcome,description,diagnostic_id,trigger_marker_at,trigger_kind)
  values ({{ dbt.current_timestamp() }}, {{ dbt_dqm.sql_string(invocation_id) }}, {{ dbt_dqm.sql_string(step_name) }},
    case when {{ ok_expression }} then 'succeeded' else 'failed' end,
    case when {{ ok_expression }} then {{ dbt_dqm.sql_string(step_name | replace('_', ' ') ~ ' succeeded') }}
      else {{ dbt_dqm.sql_string(step_name | replace('_', ' ') ~ ' failed') }} end,
    case when {{ ok_expression }} then cast(null as {{ dbt.type_string() }}) else {{ diagnostic_expression }} end,
    {{ marker }}, {{ dbt_dqm.sql_string(kind) }});
{% endmacro %}

{# --- Script generator, shared by execution and the render tests --- #}

{#- BigQuery: one step as its own job. The final select reports the outcome (and, when
    `read_marker`, the trigger marker as a fixed-format UTC string) to the executor. #}
{% macro bigquery__maintenance_step_script(step, days, events_days, read_marker=false) %}
  declare dqm_step_ok bool default false;
  declare dqm_step_txn bool default false;
  declare dqm_stage_failed bool default false;
  declare dqm_trigger_marker timestamp default null;
  {% if read_marker %}{{ dbt_dqm.bigquery__maintenance_marker_sql() }}{% endif %}
  begin
    {{ dbt_dqm.ready_sql() }}
    {{ dbt_dqm.maintenance_step_prelude_sql(step) }}
    {% if step.transactional %}begin transaction; set dqm_step_txn = true;{% endif %}
    {{ dbt_dqm.maintenance_step_sql(step, days, events_days) }}
    {% if step.transactional %}set dqm_step_txn = false; commit transaction;{% endif %}
    set dqm_step_ok = true;
  exception when error then
    {#- BigQuery raises an error no handler can catch when ROLLBACK runs without an open
        transaction, so roll back only while this step's transaction is known to be open. The
        flag is cleared just before COMMIT: whatever a failed commit leaves open ends with this
        job, and the outcomes are logged by a separate job. #}
    if dqm_step_txn then
      rollback transaction;
    end if;
  end;
  select dqm_step_ok as ok, @@script.job_id as job_id,
    format_timestamp('%Y-%m-%dT%H:%M:%E6SZ', dqm_trigger_marker, 'UTC') as trigger_marker_at;
{% endmacro %}

{#- BigQuery: the separate job that records every step's outcome after the last step. Each row is
    protected on its own, so one failed insert doesn't drop the others. `outcomes` is a list of
    {step, ok, job_id}. #}
{% macro bigquery__maintenance_log_script(outcomes, marker, kind) %}
  {% for outcome in outcomes %}
    begin
      {{ dbt_dqm.maintenance_log_insert_sql(outcome.step, 'true' if outcome.ok else 'false', dbt_dqm.sql_string(outcome.job_id) if outcome.job_id else 'cast(null as string)', marker, kind) }}
    exception when error then
      select 'maintenance outcome not logged' as maintenance_note;
    end;
  {% endfor %}
{% endmacro %}

{#- Postgres: the whole run as one locked transaction. The marker is read once into a temporary
    table before any step, so pruning can't change the attribution of later outcome rows. #}
{% macro postgres__maintenance_script(steps, days, events_days, kind) %}
  begin;
  {{ dbt_dqm.reconcile_lock() }}
  create temporary table dqm_maintenance_attribution (trigger_marker_at {{ dbt.type_timestamp() }}) on commit drop;
  do $dqm_attribution$ begin
    {% if kind == 'automatic' %}
      insert into dqm_maintenance_attribution select {{ dbt_dqm.maintenance_trigger_marker_sql(invocation_id) }};
    {% else %}
      insert into dqm_maintenance_attribution values (null);
    {% endif %}
  exception when others then
    insert into dqm_maintenance_attribution values (null);
  end $dqm_attribution$;
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
        {{ dbt_dqm.maintenance_log_insert_sql(step.name, 'dqm_step_ok', 'dqm_step_state',
          '(select trigger_marker_at from dqm_maintenance_attribution)', kind) }}
      exception when others then
        null;
      end;
    end $dqm_maintenance$;
  {% endfor %}
  commit;
{% endmacro %}

{# --- Executor --- #}

{#- Runs optional maintenance and returns what it observed: a list of {step, ok}. `ok` is the
    step job's own result on BigQuery, and none on Postgres, where outcomes are only known from
    the log. `kind` is 'automatic' or 'manual'. #}
{% macro maintenance_run(days, events_days, kind) %}
  {% set observed = [] %}
  {% set steps = dbt_dqm.maintenance_steps(days, events_days) %}
  {% if not execute or dbt_dqm.empty_mode() or not steps %}{{ return(observed) }}{% endif %}
  {% if target.type == 'bigquery' %}
    {#- A namespace: a plain `set` inside the loop wouldn't outlive it. #}
    {% set run = namespace(marker='cast(null as timestamp)') %}
    {% set outcomes = [] %}
    {% for step in steps %}
      {% set read_marker = loop.first and kind == 'automatic' %}
      {% set result = run_query(dbt_dqm.bigquery__maintenance_step_script(step, days, events_days, read_marker)) %}
      {% set row = result.rows[0] if result.rows | length == 1 else none %}
      {% set ok = row is not none and row[0] == true %}
      {% set job_id = row[1] if row is not none else none %}
      {% if job_id is not none and not modules.re.match('^[A-Za-z0-9_.:-]+$', job_id | string) %}{% set job_id = none %}{% endif %}
      {% if read_marker and row is not none and row[2] is not none
            and modules.re.match('^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[.][0-9]{6}Z$', row[2] | string) %}
        {% set run.marker = "timestamp '" ~ row[2] ~ "'" %}
      {% endif %}
      {% do outcomes.append({'step': step.name, 'ok': ok, 'job_id': job_id}) %}
      {% do observed.append({'step': step.name, 'ok': ok}) %}
    {% endfor %}
    {% do run_query(dbt_dqm.bigquery__maintenance_log_script(outcomes, run.marker, kind)) %}
  {% else %}
    {% do run_query(dbt_dqm.postgres__maintenance_script(steps, days, events_days, kind)) %}
    {% for step in steps %}{% do observed.append({'step': step.name, 'ok': none}) %}{% endfor %}
  {% endif %}
  {{ return(observed) }}
{% endmacro %}
