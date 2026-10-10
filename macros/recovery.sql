{% macro recovery_begin() %}
begin{% if target.type=='bigquery' %} transaction{% endif %};
{{ dbt_dqm.reconcile_lock() }}
{{ dbt_dqm.ready_sql() }}
{% endmacro %}
{% macro recovery_commit() %}commit{% if target.type=='bigquery' %} transaction{% endif %};{% endmacro %}

{% macro dqm_skip_late_evidence(items,reason) %}
  {% if dbt_dqm.empty_mode() %}{{ return('') }}{% endif %}
  {% if not items or reason is not string or not reason|trim %}{{ exceptions.raise_compiler_error('items and a nonblank reason are required.') }}{% endif %}
  {% set values=[] %}{% set identities=[] %}{% for item in items %}
    {% if not item.get('test_unique_id') or not item.get('invocation_id') %}{{ exceptions.raise_compiler_error('Each item needs test_unique_id and invocation_id.') }}{% endif %}
    {% set identity=[item.test_unique_id,item.invocation_id] %}
    {% if identity in identities %}{{ exceptions.raise_compiler_error('Late-evidence items must be distinct.') }}{% endif %}
    {% do identities.append(identity) %}
    {% do values.append('select ' ~ dbt_dqm.sql_string(item.test_unique_id) ~ ' as test_unique_id, ' ~ dbt_dqm.sql_string(item.invocation_id) ~ ' as invocation_id') %}
  {% endfor %}
  {% set requested=values|join(' union all ') %}
  {% set eligible %}
    select execution.* from {{ dbt_dqm.dqm_relation('dqm_test_executions') }} execution
    inner join ({{ requested }}) requested using(test_unique_id,invocation_id)
    inner join {{ dbt_dqm.dqm_relation('dqm_reconciliation_state') }} state using(test_unique_id)
    where {{ dbt_dqm.conclusive_sql() }} and
      (execution.captured_at<state.captured_at or (execution.captured_at=state.captured_at and execution.invocation_id<=state.invocation_id))
      and not exists(select 1 from {{ dbt_dqm.dqm_relation('dqm_reconciliation_receipts') }} receipt
        where receipt.test_unique_id=execution.test_unique_id and receipt.invocation_id=execution.invocation_id)
  {% endset %}
  {% set sql %}
    {{ dbt_dqm.recovery_begin() }}
    {{ dbt_dqm.assert_sql('(select count(*) from (' ~ eligible ~ ') eligible)=' ~ items|length,
      dbt_dqm.sql_string('Every requested execution must be distinct, conclusive, late and unreceipted.')) }}
    insert into {{ dbt_dqm.dqm_relation('dqm_issue_events') }}
      (event_id,test_unique_id,invocation_id,event_type,event_at,reason,actor)
      select {{ dbt_dqm.event_id_sql('test_unique_id','cast(null as ' ~ dbt.type_string() ~ ')','invocation_id',"'EVIDENCE_SKIPPED'") }},
        test_unique_id,invocation_id,'EVIDENCE_SKIPPED',{{ dbt.current_timestamp() }},{{ dbt_dqm.sql_string(reason) }},
        {% if target.type=='bigquery' %}session_user(){% else %}current_user{% endif %}
      from ({{ eligible }}) eligible;
    insert into {{ dbt_dqm.dqm_relation('dqm_reconciliation_receipts') }}
      (test_unique_id,invocation_id,captured_at,run_id,outcome,reason,actor,recorded_at)
      select test_unique_id,invocation_id,captured_at,{{ dbt_dqm.sql_string(invocation_id) }},'skipped_late',{{ dbt_dqm.sql_string(reason) }},
        {% if target.type=='bigquery' %}session_user(){% else %}current_user{% endif %},{{ dbt.current_timestamp() }} from ({{ eligible }}) eligible;
    update {{ dbt_dqm.dqm_relation('dqm_reconciliation_control') }} set generation=generation+1 where true;
    {{ dbt_dqm.recovery_commit() }}
  {% endset %}
  {% if execute %}{% do run_query(sql) %}{% endif %}
{% endmacro %}

{% macro dqm_abandon_runs(older_than_minutes=60) %}
  {% if dbt_dqm.empty_mode() %}{{ return('') }}{% endif %}
  {% if older_than_minutes is boolean or older_than_minutes is not number or older_than_minutes|int!=older_than_minutes or older_than_minutes<=0 %}{{ exceptions.raise_compiler_error('older_than_minutes must be a positive integer.') }}{% endif %}
  {% set sql %}{{ dbt_dqm.recovery_begin() }}
    update {{ dbt_dqm.dqm_relation('dqm_reconciliation_runs') }} set status='abandoned',completed_at={{ dbt.current_timestamp() }}
    where status='started' and started_at < {{ dbt_dqm.timestamp_add('minute',-older_than_minutes,dbt.current_timestamp()) }};
    update {{ dbt_dqm.dqm_relation('dqm_reconciliation_control') }} set generation=generation+1 where true;
    {{ dbt_dqm.recovery_commit() }}
  {% endset %}
  {% if execute %}{% do run_query(sql) %}{% do dbt_dqm.dqm_cleanup_stages() %}{% endif %}
{% endmacro %}

{#- Drops abandoned stages and completed ones older than 24 hours. The recovery path
    (dqm_cleanup_stages, dqm_abandon_runs) is tolerant: a failed drop is skipped. Maintenance
    passes fail_on_error=true: every eligible stage is still attempted, then the step fails with a
    fixed message if any drop failed, so the outcome is logged as failed and run pruning keeps the
    stage's run for a retry. Needs `dqm_stage_failed` declared by the calling script. #}
{% macro cleanup_stages_sql(fail_on_error=false) %}
  {% if target.type=='bigquery' %}
    for stage in (select stage_relation from {{ dbt_dqm.dqm_relation('dqm_reconciliation_runs') }}
      where status='abandoned' or (status='completed' and completed_at <= timestamp_sub(current_timestamp(),interval 24 hour))) do
      if strpos(stage.stage_relation,'dqm_reconcile_stage_') > 0 then
        begin
          {{ dbt_dqm.maintenance_stage_drop_sql('stage.stage_relation') }}
        exception when error then
          {% if fail_on_error %}set dqm_stage_failed = true;{% else %}select 'Warning: DQM stage cleanup failed' as warning;{% endif %}
        end;
      end if;
    end for;
    {% if fail_on_error %}if dqm_stage_failed then raise using message = 'DQM stage drop failed'; end if;{% endif %}
  {% endif %}
{% endmacro %}
{% macro dqm_cleanup_stages() %}
  {% if execute and not dbt_dqm.empty_mode() and target.type=='bigquery' %}{% do run_query(dbt_dqm.cleanup_stages_sql()) %}{% endif %}
{% endmacro %}

{% macro maintenance_retention_days(retention_days=none) %}
  {% set days=retention_days if retention_days is not none else dbt_dqm.positive_integer_var('dbt_dqm_retention_days',none) %}
  {% if days is not none and (days is boolean or days is not number or days|int!=days or days<=0) %}{{ exceptions.raise_compiler_error('retention_days must be a positive integer.') }}{% endif %}
  {{ return(days) }}
{% endmacro %}

{#- Explicit maintenance. Steps run protected (one failure doesn't stop the others); afterwards
    every expected step must have exactly a succeeded outcome recorded for this invocation, and
    on BigQuery the step job itself must have reported success. A missing, failed or ambiguous
    outcome raises: success is never inferred from the absence of failure rows. With no
    configured steps this is a no-op. #}
{% macro cleanup_dqm_logs(retention_days=none) %}
  {% if execute and not dbt_dqm.empty_mode() %}
    {% set days = dbt_dqm.maintenance_retention_days(retention_days) %}
    {% set events_days = dbt_dqm.positive_integer_var('dbt_dqm_event_retention_days',none) %}
    {% set observed = dbt_dqm.maintenance_run(days, events_days, 'manual') %}
    {% if observed | length > 0 %}
      {% set logged = run_query('select step, outcome, diagnostic_id from ' ~ dbt_dqm.dqm_relation('dqm_maintenance_log')
        ~ ' where invocation_id=' ~ dbt_dqm.sql_string(invocation_id) ~ " and trigger_kind='manual' order by step") %}
      {% set problems = [] %}
      {% for item in observed %}
        {% set outcomes = [] %}{% set diagnostics = [] %}
        {% for row in logged.rows if row[0] == item.step %}
          {% do outcomes.append(row[1]) %}{% if row[2] %}{% do diagnostics.append(row[2]) %}{% endif %}
        {% endfor %}
        {% if outcomes | length == 0 %}
          {% do problems.append(item.step ~ ' (outcome not recorded)') %}
        {% elif outcomes | unique | list != ['succeeded'] %}
          {% do problems.append(item.step ~ ' (failed' ~ (', diagnostic ' ~ diagnostics | join('/') if diagnostics else '') ~ ')') %}
        {% elif item.ok is false %}
          {% do problems.append(item.step ~ ' (step job reported failure)') %}
        {% endif %}
      {% endfor %}
      {% if problems %}
        {{ exceptions.raise_compiler_error('DQM maintenance did not complete: ' ~ problems | join(', ') ~ '. See dqm_maintenance_log.') }}
      {% endif %}
    {% endif %}
  {% endif %}
{% endmacro %}

{#- Whether this invocation wrote a maintenance trigger: captured tracked-test results (capture
    records every tracked result of dbt test/build) or completed a reconciliation. Only these
    invocations advance the activity markers that dqm_maintenance_health compares outcomes to,
    so other package models (public views, health views) never trigger maintenance. #}
{% macro maintenance_triggered(results) %}
  {% for result in (results or []) %}
    {% if flags.WHICH in ['test','build'] and dbt_dqm.tracked_test(result.node) %}{{ return(true) }}{% endif %}
    {% if result.node.unique_id == 'model.dbt_dqm.dqm_reconcile' and result.status == 'success' %}{{ return(true) }}{% endif %}
  {% endfor %}
  {{ return(false) }}
{% endmacro %}

{#- Optional maintenance after run/build/test, at most once per invocation (this on-run-end hook
    runs once). It executes the steps itself and returns no SQL. See macros/maintenance.sql for
    what it guarantees. #}
{% macro retention_hook(results=none) %}
  {% if not execute or dbt_dqm.empty_mode() or flags.WHICH not in ['run','build','test'] %}{{ return('') }}{% endif %}
  {% if target.type!='bigquery' and var('dbt_dqm_retention_days',none) is none and var('dbt_dqm_event_retention_days',none) is none %}{{ return('') }}{% endif %}
  {#- Both triggers (capture and reconciliation) ran setup earlier in this invocation, so the
      maintenance tables exist; the steps and log writes are protected regardless. #}
  {% if not dbt_dqm.maintenance_triggered(results) %}{{ return('') }}{% endif %}
  {% do dbt_dqm.maintenance_run(dbt_dqm.maintenance_retention_days(), dbt_dqm.positive_integer_var('dbt_dqm_event_retention_days',none), 'automatic') %}
  {{ return('') }}
{% endmacro %}
