{% macro recovery_begin() %}
begin{% if target.type=='bigquery' %} transaction{% endif %};
{{ dbt_dqm.reconcile_lock() }}
{{ dbt_dqm.ready_sql() }}
{% endmacro %}
{% macro recovery_commit() %}commit{% if target.type=='bigquery' %} transaction{% endif %};{% endmacro %}

{% macro dqm_skip_late_evidence(items,reason) %}
  {% if dbt_dqm.empty_mode() %}{{ return('') }}{% endif %}
  {% if not items or reason is not string or not reason|trim %}{{ exceptions.raise_compiler_error('items and a nonblank reason are required.') }}{% endif %}
  {% set values=[] %}{% for item in items %}
    {% if not item.get('test_unique_id') or not item.get('invocation_id') %}{{ exceptions.raise_compiler_error('Each item needs test_unique_id and invocation_id.') }}{% endif %}
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
    where status='started' and started_at < {{ dbt.dateadd('minute',-older_than_minutes,dbt.current_timestamp()) }};
    update {{ dbt_dqm.dqm_relation('dqm_reconciliation_control') }} set generation=generation+1 where true;
    {{ dbt_dqm.recovery_commit() }}
  {% endset %}
  {% if execute %}{% do run_query(sql) %}{% do dbt_dqm.dqm_cleanup_stages() %}{% endif %}
{% endmacro %}

{% macro cleanup_stages_sql() %}
  {% if target.type=='bigquery' %}
    for stage in (select stage_relation from {{ dbt_dqm.dqm_relation('dqm_reconciliation_runs') }}
      where status='abandoned' or (status='completed' and completed_at <= timestamp_sub(current_timestamp(),interval 24 hour))) do
      if strpos(stage.stage_relation,'dqm_reconcile_stage_') > 0 then
        begin
          execute immediate concat('drop table if exists ',stage.stage_relation);
        exception when error then select 'Warning: DQM stage cleanup failed' as warning;
        end;
      end if;
    end for;
  {% endif %}
{% endmacro %}
{% macro dqm_cleanup_stages() %}
  {% if execute and not dbt_dqm.empty_mode() and target.type=='bigquery' %}{% do run_query(dbt_dqm.cleanup_stages_sql()) %}{% endif %}
{% endmacro %}

{% macro cleanup_dqm_logs_sql(retention_days=none) %}
  {% if not execute or dbt_dqm.empty_mode() %}{{ return('') }}{% endif %}
  {% set days=retention_days if retention_days is not none else dbt_dqm.positive_integer_var('dbt_dqm_retention_days',none) %}
  {% if days is not none and (days is boolean or days is not number or days|int!=days or days<=0) %}{{ exceptions.raise_compiler_error('retention_days must be a positive integer.') }}{% endif %}
  {% set events_days=dbt_dqm.positive_integer_var('dbt_dqm_event_retention_days',none) %}
  {% set sql %}{{ dbt_dqm.recovery_begin() }}
    {% if days is not none %}
      update {{ dbt_dqm.dqm_relation('dqm_reconciliation_control') }} set raw_pruned_before=case when raw_pruned_before > {{ dbt.dateadd('day',-days,dbt.current_timestamp()) }} then raw_pruned_before else {{ dbt.dateadd('day',-days,dbt.current_timestamp()) }} end,generation=generation+1 where true;
      {% set eligible %}select execution.test_unique_id,execution.invocation_id
      from {{ dbt_dqm.dqm_relation('dqm_test_executions') }} execution
      inner join {{ dbt_dqm.dqm_relation('dqm_reconciliation_receipts') }} receipt using(test_unique_id,invocation_id)
      where execution.captured_at < {{ dbt.dateadd('day',-days,dbt.current_timestamp()) }}
      and not exists(select 1 from {{ dbt_dqm.dqm_relation('dqm_reconciliation_inputs') }} input
        inner join {{ dbt_dqm.dqm_relation('dqm_reconciliation_runs') }} run using(run_id)
        where run.status='started' and input.test_unique_id=execution.test_unique_id and input.invocation_id=execution.invocation_id)
      {% endset %}
      {% if target.type=='postgres' %}create temporary table dqm_prune on commit drop as {{ eligible }};
      {% else %}create temp table dqm_prune as {{ eligible }};{% endif %}
      {% for name in ['dqm_issue_observations','dqm_reconciliation_receipts','dqm_test_executions'] %}
        delete from {{ dbt_dqm.dqm_relation(name) }} target where exists(select 1 from dqm_prune prune
          where prune.test_unique_id=target.test_unique_id and prune.invocation_id=target.invocation_id);
      {% endfor %}
    {% endif %}
    {% if events_days is not none %}delete from {{ dbt_dqm.dqm_relation('dqm_issue_events') }} where event_at < {{ dbt.dateadd('day',-events_days,dbt.current_timestamp()) }};{% endif %}
    {{ dbt_dqm.recovery_commit() }}
  {% endset %}
  {{ sql }}
  {{ dbt_dqm.cleanup_stages_sql() }}
  {% if days is not none %}
    {% set prune_runs %}
      {{ dbt_dqm.recovery_begin() }}
      delete from {{ dbt_dqm.dqm_relation('dqm_reconciliation_inputs') }} input where exists(select 1 from {{ dbt_dqm.dqm_relation('dqm_reconciliation_runs') }} run
        where run.run_id=input.run_id and run.status!='started' and run.completed_at < {{ dbt.dateadd('day',-days,dbt.current_timestamp()) }});
      delete from {{ dbt_dqm.dqm_relation('dqm_reconciliation_runs') }} where status!='started' and completed_at < {{ dbt.dateadd('day',-days,dbt.current_timestamp()) }};
      {{ dbt_dqm.recovery_commit() }}
    {% endset %}{{ prune_runs }}
  {% endif %}
{% endmacro %}

{% macro cleanup_dqm_logs(retention_days=none) %}
  {% if execute and not dbt_dqm.empty_mode() %}{% do run_query(dbt_dqm.cleanup_dqm_logs_sql(retention_days)) %}{% endif %}
{% endmacro %}
{% macro retention_hook() %}
  {% if not execute or dbt_dqm.empty_mode() or flags.WHICH not in ['run','build','test'] %}{{ return('') }}{% endif %}
  {% if var('dbt_dqm_retention_days',none) is none and var('dbt_dqm_event_retention_days',none) is none %}{{ return('') }}{% endif %}
  {% set relation=dbt_dqm.dqm_relation('dqm_reconciliation_control') %}
  {% if adapter.get_relation(database=relation.database,schema=relation.schema,identifier=relation.identifier) is not none %}
    {{ dbt_dqm.cleanup_dqm_logs_sql() }}
  {% endif %}
{% endmacro %}
