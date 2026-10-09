{#
  Reconciliation protocol, run by the dqm_reconcile model's hooks:

    pre-hook  reconcile_lock (Postgres)  serialize runs and initialization
    pre-hook  reconcile_pre              setup -> freeze inputs -> late-evidence check -> stage
    post-hook apply_reconciliation       guarded apply of the staged change set

  On Postgres all of it runs in dbt's model transaction. On BigQuery each phase is a script with
  its own transaction, fenced by the control generation and the run's status.
#}

{# Postgres stages in a session temp table; BigQuery needs a real table that outlives the
   pre-hook script, named per run so concurrent runs can't apply each other's changes. #}
{% macro stage_relation() %}
  {% if target.type == 'postgres' %}
    {{ return('pg_temp.dqm_stage') }}
  {% else %}
    {{ return(dbt_dqm.dqm_relation('dqm_reconcile_stage_' ~ invocation_id | replace('-', ''))) }}
  {% endif %}
{% endmacro %}

{% macro tracked_ids_sql() %}
  {% set ids = [] %}
  {% for node in graph.nodes.values() %}
    {% if dbt_dqm.tracked_test(node) %}{% do ids.append(dbt_dqm.sql_string(node.unique_id)) %}{% endif %}
  {% endfor %}
  {{ return(ids | join(',') if ids else "''") }}
{% endmacro %}

{# Executions that are lifecycle evidence: collected without error and a conclusive dbt status. #}
{% macro conclusive_sql(alias='execution') %}
  {{ alias }}.collection_status in ('success','not_applicable') and lower({{ alias }}.test_status) in ('pass','warn','fail')
{% endmacro %}

{% macro reconcile_pre() %}
  {% if not execute or dbt_dqm.empty_mode() %}{{ return('') }}{% endif %}
  {% if var('dbt_dqm_reconcile_lookback_days', none) is not none %}
    {% do exceptions.warn('dbt_dqm_reconcile_lookback_days is deprecated and ignored; all unreceipted evidence is processed.') %}
  {% endif %}
  {% set runs = dbt_dqm.dqm_relation('dqm_reconciliation_runs') %}
  {% set inputs = dbt_dqm.dqm_relation('dqm_reconciliation_inputs') %}
  {% set control = dbt_dqm.dqm_relation('dqm_reconciliation_control') %}
  {% set run_id = dbt_dqm.sql_string(invocation_id) %}
  {% set timeout_minutes = dbt_dqm.positive_integer_var('dbt_dqm_reconcile_timeout_minutes', 60) %}

  {#- 1. Setup: install marker, migrations, grants. Postgres then locks the occurrence table so
      reviewer writes and other runs wait; BigQuery can only detect another active run. #}
  {{ dbt_dqm.setup_sql() }}
  {% if target.type == 'postgres' %}
    lock table {{ dbt_dqm.dqm_relation('dqm_issue_occurrences') }} in exclusive mode;
  {% else %}
    {{ dbt_dqm.assert_sql(
      'not exists(select 1 from ' ~ runs ~ " where status='started' and started_at > timestamp_sub(current_timestamp(), interval " ~ timeout_minutes ~ ' minute))',
      dbt_dqm.sql_string('Another DQM reconciliation is active; retry after it finishes.')) }}
    begin
    begin transaction;
  {% endif %}

  {#- 2. Freeze: record the run with the generation it read, and the exact executions it will
      process (every conclusive, unreceipted execution of a tracked test). #}
  insert into {{ runs }} (run_id,generation_at_freeze,stage_relation,status,started_at)
    select {{ run_id }}, generation, {{ dbt_dqm.sql_string(dbt_dqm.stage_relation() | string) }}, 'started', {{ dbt.current_timestamp() }}
    from {{ control }};
  insert into {{ inputs }} (run_id,test_unique_id,invocation_id,captured_at)
    select {{ run_id }}, execution.test_unique_id,execution.invocation_id,execution.captured_at
    from {{ dbt_dqm.dqm_relation('dqm_test_executions') }} execution
    where {{ dbt_dqm.conclusive_sql() }} and execution.test_unique_id in ({{ dbt_dqm.tracked_ids_sql() }})
      and not exists(select 1 from {{ dbt_dqm.dqm_relation('dqm_reconciliation_receipts') }} receipt
        where receipt.test_unique_id=execution.test_unique_id and receipt.invocation_id=execution.invocation_id);
  {% if target.type == 'bigquery' %}
    commit transaction;
    exception when error then rollback transaction; raise; end;
    begin
    {#- Capture writes a test's observations after its execution row, in the same on-run-end
        script, so observed_at >= captured_at. A day of margin covers clock differences between
        statements. The bound is a script variable so BigQuery treats it as a constant and prunes
        the observation partitions older than this run's evidence. #}
    declare dqm_observation_floor timestamp default (
      select timestamp_sub(min(captured_at), interval 1 day) from {{ inputs }}
      where run_id={{ run_id }});
  {% endif %}

  {#- 3. Late evidence: a frozen execution at or before a test's processed high-water arrived
      after a later one was applied. Stop before any lifecycle write. #}
  {% set late_list %}(select string_agg(concat(test_unique_id, '/', invocation_id), ', ') from ({{ dbt_dqm.late_inputs_sql() }}) late){% endset %}
  {{ dbt_dqm.assert_sql(
    'not exists(' ~ dbt_dqm.late_inputs_sql() ~ ')',
    "concat('Late DQM evidence: ', " ~ late_list ~ ", '. Review and use dqm_skip_late_evidence with an explicit reason.')") }}

  {#- 4. Stage the change set. Postgres has no statistics for the change set's CTEs and estimates
      them at one row, so it picks nested loops that grow with affected identities x history:
      a run archiving 20,000 issues didn't finish in ten minutes. Every join there has equality
      keys, so hash joins are always available; nested loops are disabled for this one
      statement only. docs/scale.md has the measurements across workloads. #}
  {% if target.type == 'postgres' %}
    set local enable_nestloop = off;
    create temporary table dqm_stage on commit drop as
  {% else %}
    create table {{ dbt_dqm.stage_relation() }} options(expiration_timestamp=timestamp_add(current_timestamp(), interval 24 hour)) as
  {% endif %}
    {{ dbt_dqm.reconcile_change_set_sql(invocation_id, 'dqm_observation_floor' if target.type == 'bigquery' else none) }};
  {% if target.type == 'postgres' %}
    reset enable_nestloop;
  {% else %}
    exception when error then {{ dbt_dqm.abandon_failed_run_sql() }} raise; end;
  {% endif %}
{% endmacro %}

{# Frozen inputs ordered at or before their test's processed high-water. #}
{% macro late_inputs_sql() %}
  select input.test_unique_id,input.invocation_id from {{ dbt_dqm.dqm_relation('dqm_reconciliation_inputs') }} input
  inner join {{ dbt_dqm.dqm_relation('dqm_reconciliation_state') }} state using(test_unique_id)
  where input.run_id={{ dbt_dqm.sql_string(invocation_id) }}
    and (input.captured_at<state.captured_at or (input.captured_at=state.captured_at and input.invocation_id<=state.invocation_id))
{% endmacro %}

{% macro apply_reconciliation() %}
  {% if not execute or dbt_dqm.empty_mode() %}{{ return('') }}{% endif %}
  {% set control = dbt_dqm.dqm_relation('dqm_reconciliation_control') %}
  {% set runs = dbt_dqm.dqm_relation('dqm_reconciliation_runs') %}
  {% set inputs = dbt_dqm.dqm_relation('dqm_reconciliation_inputs') %}
  {% set occ = dbt_dqm.dqm_relation('dqm_issue_occurrences') %}
  {% set run_id = dbt_dqm.sql_string(invocation_id) %}
  {% set columns = [] %}
  {% for col, kind in dbt_dqm.table_schemas()['dqm_issue_occurrences'] %}{% do columns.append(col) %}{% endfor %}
  {% set event_columns = [] %}
  {% for col, kind in dbt_dqm.table_schemas()['dqm_issue_events'] %}{% do event_columns.append(col) %}{% endfor %}
  {#- Reviewer-owned columns: never overwritten on matched rows, so concurrent edits and
      annotation versions survive. workflow_status_at_close is set once, from the target row. #}
  {% set protected = ['workflow_status','test_status','call_to_action','ticket_url','notes','poc_responsible','review_verdict','annotation_version','annotation_updated_at','workflow_status_at_close','occurrence_id'] %}
  {% set closure_snapshot = 'workflow_status_at_close=case when target.record_status=\'Active\' and {new}.record_status=\'Archived\' then target.workflow_status else target.workflow_status_at_close end' %}

  {% if target.type == 'bigquery' %}
    begin
    begin transaction;
  {% endif %}

  {#- Guard: setup is current, and no other run, migration or recovery operation changed the
      generation since this run froze its inputs. #}
  {{ dbt_dqm.ready_sql() }}
  {{ dbt_dqm.assert_sql(
    "exists(select 1 from " ~ runs ~ ' run cross join ' ~ control ~ ' control where run.run_id=' ~ run_id ~ " and run.status='started' and run.generation_at_freeze=control.generation)",
    dbt_dqm.sql_string('Stale or abandoned DQM reconciliation; rerun with a new invocation.')) }}

  {#- Occurrences: insert new ones; update package-managed lifecycle columns of existing ones. #}
  {% if target.type == 'postgres' %}
    insert into {{ occ }} as target ({{ columns | join(',') }}) select {{ columns | join(',') }} from {{ dbt_dqm.stage_relation() }} where row_kind='occurrence'
    on conflict(occurrence_id) do update set
      {% for col in columns if col not in protected %}{{ col }}=excluded.{{ col }},{% endfor %}
      {{ closure_snapshot.format(new='excluded') }};
  {% else %}
    merge {{ occ }} target using (select * from {{ dbt_dqm.stage_relation() }} where row_kind='occurrence') source on target.occurrence_id=source.occurrence_id
    when matched then update set
      {% for col in columns if col not in protected %}{{ col }}=source.{{ col }},{% endfor %}
      {{ closure_snapshot.format(new='source') }}
    when not matched then insert({{ columns | join(',') }}) values({% for col in columns %}source.{{ col }}{% if not loop.last %},{% endif %}{% endfor %});
  {% endif %}

  {#- Events (idempotent by event_id), receipts for every frozen input, the per-test high-water,
      a generation bump that fences any other frozen run, and this run's completion. #}
  {{ dbt_dqm.insert_new_rows(dbt_dqm.dqm_relation('dqm_issue_events'), "select " ~ event_columns | join(',') ~ ' from ' ~ dbt_dqm.stage_relation() ~ " where row_kind='event'", ['event_id'], event_columns) }};
  insert into {{ dbt_dqm.dqm_relation('dqm_reconciliation_receipts') }} (test_unique_id,invocation_id,captured_at,run_id,outcome,recorded_at)
    select test_unique_id,invocation_id,captured_at,run_id,'applied',{{ dbt.current_timestamp() }}
    from {{ inputs }} where run_id={{ run_id }};
  {% set state_source %}
    select test_unique_id,captured_at,invocation_id from (
      select *, row_number() over(partition by test_unique_id order by captured_at desc,invocation_id desc) as rank
      from {{ inputs }} where run_id={{ run_id }}
    ) frozen where rank=1
  {% endset %}
  {{ dbt_dqm.upsert_reconciliation_state(state_source) }};
  update {{ control }} set generation=generation+1 where true;
  update {{ runs }} set status='completed',completed_at={{ dbt.current_timestamp() }} where run_id={{ run_id }};

  {% if target.type == 'bigquery' %}
    commit transaction;
    exception when error then rollback transaction; {{ dbt_dqm.abandon_failed_run_sql() }} raise; end;
    {#- The stage stays for later cleanup (dbt still applies grants and docs to the model);
        extending its expiration is best-effort. #}
    begin
      alter table {{ dbt_dqm.stage_relation() }} set options(expiration_timestamp=timestamp_add(current_timestamp(),interval 24 hour));
    exception when error then select 'Warning: stage expiration could not be extended' as warning;
    end;
  {% endif %}
{% endmacro %}

{# Event IDs: SHA-256 of a length-prefixed, null-safe encoding, so no two field combinations
   collide. #}
{% macro event_id_sql(test_id, occurrence_id, execution_id, event_type) %}
  {% set expressions = [test_id, occurrence_id, execution_id, event_type] %}
  {% set encoded %}concat('dqm-event-v1|'{% for expr in expressions %},case when {{ expr }} is null then 'n|' else concat('s',cast(length({{ expr }}) as {{ dbt.type_string() }}),':',{{ expr }},'|') end{% endfor %}){% endset %}
  {{ dbt_dqm.sha256_hex(encoded) }}
{% endmacro %}

{# A known SQL failure releases the active-run check immediately. Lost clients still use timeout recovery. #}
{% macro abandon_failed_run_sql() %}
  begin transaction;
  if exists(select 1 from {{ dbt_dqm.dqm_relation('dqm_reconciliation_runs') }} where run_id={{ dbt_dqm.sql_string(invocation_id) }} and status='started') then
    update {{ dbt_dqm.dqm_relation('dqm_reconciliation_runs') }} set status='abandoned',completed_at=current_timestamp()
      where run_id={{ dbt_dqm.sql_string(invocation_id) }} and status='started';
    update {{ dbt_dqm.dqm_relation('dqm_reconciliation_control') }} set generation=generation+1 where true;
  end if;
  commit transaction;
{% endmacro %}
