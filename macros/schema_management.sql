{% macro empty_mode() %}{{ return(flags.EMPTY | default(false)) }}{% endmacro %}

{% macro assert_sql(condition, message) %}
  {% if target.type == 'postgres' %}
    do $dqm$ begin if not coalesce(({{ condition }}), false) then
      raise exception '%', {{ message }}; end if; end $dqm$;
  {% elif target.type == 'bigquery' %}
    select if(coalesce(({{ condition }}), false), true, error({{ message }}));
  {% else %}{{ dbt_dqm.unsupported_adapter_error('assertions') }}{% endif %}
{% endmacro %}

{% macro reconcile_lock() %}
  {% if not dbt_dqm.empty_mode() and target.type == 'postgres' %}
    select pg_advisory_xact_lock(hashtext({{ dbt_dqm.sql_string(dbt_dqm.installation_lock_key()) }}));
  {% endif %}
{% endmacro %}

{% macro migrations() %}{{ return(adapter.dispatch('migrations', 'dbt_dqm')()) }}{% endmacro %}
{% macro default__migrations() %}
  {{ return([
    {'id': '0001_initial', 'description': 'Fresh 0.2 schema', 'apply': 'initial_schema_apply', 'verify': 'initial_schema_verify'},
    {'id': '0002_event_payload_mode', 'description': 'Event payload mode, changed columns and digests', 'apply': 'event_payload_mode_apply', 'backfill': 'event_payload_mode_backfill', 'verify': 'event_payload_mode_verify'},
    {'id': '0003_app_change_staging', 'description': 'Package-owned staging table for BigQuery app edits', 'apply': 'app_change_staging_apply', 'verify': 'app_change_staging_verify'},
    {'id': '0004_app_staging_safety', 'description': 'Isolated app uploads with bounded lifetime', 'apply': 'app_staging_safety_apply', 'backfill': 'app_staging_safety_backfill', 'verify': 'app_staging_safety_verify'},
    {'id': '0005_maintenance_log', 'description': 'Outcome log for optional maintenance', 'apply': 'maintenance_log_apply', 'verify': 'maintenance_log_verify'},
    {'id': '0006_maintenance_attribution', 'description': 'Trigger attribution for maintenance outcomes', 'apply': 'maintenance_attribution_apply', 'verify': 'maintenance_attribution_verify'},
    {'id': '0007_browse_keyset_index', 'description': 'Keyset index for warehouse browsing', 'apply': 'browse_keyset_index_apply', 'verify': 'browse_keyset_index_verify'}
  ]) }}
{% endmacro %}

{% macro capture_tables_ddl() %}
  {% for name, columns in dbt_dqm.table_schemas().items() if name not in ['dqm_annotation_changes','dqm_missed_issues'] %}
    create table if not exists {{ dbt_dqm.dqm_relation(name) }} (
      {% for column, kind in columns %}
        {{ adapter.quote(column) }} {{ context['dbt']['type_' ~ kind]() }}
        {% if column in dbt_dqm.table_keys()[name] %}not null{% endif %}{% if not loop.last %},{% endif %}
      {% endfor %}
      {% if target.type == 'postgres' %}, primary key ({{ dbt_dqm.table_keys()[name] | join(', ') }}){% endif %}
    )
    {% if target.type == 'bigquery' and name in ['dqm_test_executions', 'dqm_issue_observations', 'dqm_issue_events'] %}
      partition by date({{ 'captured_at' if name == 'dqm_test_executions' else 'observed_at' if name == 'dqm_issue_observations' else 'event_at' }}) cluster by test_unique_id
    {% endif %};
  {% endfor %}
  {% if target.type == 'postgres' %}
    create index if not exists {{ adapter.quote(dbt_dqm.index_name('dqm_issue_occurrences','identity_history')) }} on {{ dbt_dqm.dqm_relation('dqm_issue_occurrences') }} (test_unique_id, unique_id, occurrence_number);
    create index if not exists {{ adapter.quote(dbt_dqm.index_name('dqm_issue_occurrences','active_identity')) }} on {{ dbt_dqm.dqm_relation('dqm_issue_occurrences') }} (test_unique_id, unique_id) where record_status='Active';
    create index if not exists {{ adapter.quote(dbt_dqm.index_name('dqm_test_executions','execution_order')) }} on {{ dbt_dqm.dqm_relation('dqm_test_executions') }} (test_unique_id, captured_at, invocation_id);
    create index if not exists {{ adapter.quote(dbt_dqm.index_name('dqm_issue_observations','identity')) }} on {{ dbt_dqm.dqm_relation('dqm_issue_observations') }} (test_unique_id, unique_id, invocation_id);

  {% endif %}
{% endmacro %}
{% macro default__initial_schema_apply() %}{{ dbt_dqm.capture_tables_ddl() }}{% endmacro %}

{#- 0002: additive event columns. Fresh installs already have them from 0001, so every step is
    idempotent. Existing events were written with full payloads. #}
{% macro event_payload_columns() %}{{ return(['payload_mode', 'changed_columns', 'previous_payload_digest', 'payload_digest']) }}{% endmacro %}
{% macro default__event_payload_mode_apply() %}
  {% for column in dbt_dqm.event_payload_columns() %}
    alter table {{ dbt_dqm.dqm_relation('dqm_issue_events') }} add column if not exists {{ column }} {{ dbt.type_string() }};
  {% endfor %}
{% endmacro %}
{% macro default__event_payload_mode_backfill() %}
  update {{ dbt_dqm.dqm_relation('dqm_issue_events') }} set payload_mode = 'full'
  where payload_mode is null and event_type != 'EVIDENCE_SKIPPED';
{% endmacro %}
{% macro default__event_payload_mode_verify() %}
  {% set names = [] %}{% for column in dbt_dqm.event_payload_columns() %}{% do names.append(dbt_dqm.sql_string(column)) %}{% endfor %}
  {{ dbt_dqm.assert_sql('(select count(*) from ' ~ dbt_dqm.columns_catalog() ~ ' where table_schema=' ~ dbt_dqm.sql_string(dbt_dqm.dqm_relation('dqm_issue_events').schema) ~ " and table_name=" ~ dbt_dqm.sql_string(dbt_dqm.dqm_relation('dqm_issue_events').identifier) ~ " and lower(column_name) in (" ~ names|join(',') ~ ')) = ' ~ names|length,
      dbt_dqm.sql_string('Event payload columns are missing. Rerun setup with the current package.')) }}
{% endmacro %}

{#- 0003: the BigQuery review app stages annotation batches in this table before its
    transactional apply. Setup creates it so reviewers only need row access to it, not
    table-create rights on the whole DQM dataset. Postgres applies edits directly. #}
{% macro default__app_change_staging_apply() %}
  {% if target.type == 'bigquery' %}
    create table if not exists {{ dbt_dqm.dqm_relation('dqm_app_change_staging') }} (
      batch_id string, occurrence_id string, field_name string, old_value string,
      new_value string, changed_at timestamp, changed_by string, base_annotation_version int64,
      installation_id string, upload_id string, staged_at timestamp default current_timestamp()
    );
    {% for column, kind in [('base_annotation_version','int64'),('installation_id','string')] %}
      if not exists(select 1 from {{ dbt_dqm.columns_catalog() }}
        where table_name={{ dbt_dqm.sql_string(dbt_dqm.dqm_relation('dqm_app_change_staging').identifier) }} and lower(column_name)={{ dbt_dqm.sql_string(column) }}) then
        alter table {{ dbt_dqm.dqm_relation('dqm_app_change_staging') }} add column {{ column }} {{ kind }};
      end if;
    {% endfor %}
  {% endif %}
{% endmacro %}
{% macro default__app_change_staging_verify() %}
  {% if target.type == 'bigquery' %}
    {{ dbt_dqm.assert_sql(dbt_dqm.table_exists_sql('dqm_app_change_staging'), dbt_dqm.sql_string('dqm_app_change_staging is missing. Rerun setup with the current package.')) }}
  {% endif %}
{% endmacro %}

{#- 0004 is additive. Existing abandoned rows get a bounded grace period at migration time;
    new JSON loads omit staged_at and use the warehouse default, independent of client clocks. #}
{% macro default__app_staging_safety_apply() %}
  {% if target.type == 'bigquery' %}
    {% for column, kind in [('upload_id','string'),('staged_at','timestamp')] %}
      if not exists(select 1 from {{ dbt_dqm.columns_catalog() }}
        where table_name={{ dbt_dqm.sql_string(dbt_dqm.dqm_relation('dqm_app_change_staging').identifier) }} and lower(column_name)={{ dbt_dqm.sql_string(column) }}) then
        alter table {{ dbt_dqm.dqm_relation('dqm_app_change_staging') }} add column {{ column }} {{ kind }};
      end if;
    {% endfor %}
    if not exists(select 1 from {{ dbt_dqm.columns_catalog() }} where table_name={{ dbt_dqm.sql_string(dbt_dqm.dqm_relation('dqm_app_change_staging').identifier) }}
      and lower(column_name)='staged_at' and upper(coalesce(column_default,''))='CURRENT_TIMESTAMP()') then
      alter table {{ dbt_dqm.dqm_relation('dqm_app_change_staging') }} alter column staged_at set default current_timestamp();
    end if;
  {% endif %}
{% endmacro %}
{% macro default__app_staging_safety_backfill() %}
  {% if target.type == 'bigquery' %}
    update {{ dbt_dqm.dqm_relation('dqm_app_change_staging') }} set staged_at=current_timestamp() where staged_at is null;
  {% endif %}
{% endmacro %}
{% macro default__app_staging_safety_verify() %}
  {% if target.type == 'bigquery' %}
    {{ dbt_dqm.assert_sql('(select count(*) from ' ~ dbt_dqm.columns_catalog() ~ " where table_name=" ~ dbt_dqm.sql_string(dbt_dqm.dqm_relation('dqm_app_change_staging').identifier) ~ " and column_name in ('upload_id','staged_at'))=2",
      "'App staging safety columns are missing. Rerun setup.'") }}
    {{ dbt_dqm.assert_sql("exists(select 1 from " ~ dbt_dqm.columns_catalog() ~ " where table_name=" ~ dbt_dqm.sql_string(dbt_dqm.dqm_relation('dqm_app_change_staging').identifier) ~ " and column_name='staged_at' and lower(column_default)='current_timestamp()')",
      "'App staging timestamp default is missing. Rerun setup.'") }}
    {{ dbt_dqm.assert_sql('not exists(select 1 from ' ~ dbt_dqm.dqm_relation('dqm_app_change_staging') ~ ' where staged_at is null)',
      "'App staging timestamp backfill is incomplete. Rerun setup.'") }}
  {% endif %}
{% endmacro %}

{#- 0005: append-only outcomes of optional maintenance (macros/maintenance.sql). Not part of
    table_schemas(), so fresh installs get it here too. No warehouse error text is stored. #}
{% macro default__maintenance_log_apply() %}
  create table if not exists {{ dbt_dqm.dqm_relation('dqm_maintenance_log') }} (
    logged_at {{ dbt.type_timestamp() }} not null,
    invocation_id {{ dbt.type_string() }} not null,
    step {{ dbt.type_string() }} not null,
    outcome {{ dbt.type_string() }} not null,
    description {{ dbt.type_string() }},
    diagnostic_id {{ dbt.type_string() }}
  );
{% endmacro %}
{% macro default__maintenance_log_verify() %}
  {{ dbt_dqm.assert_sql(dbt_dqm.table_exists_sql('dqm_maintenance_log'),
    dbt_dqm.sql_string('dqm_maintenance_log is missing. Rerun setup with the current package.')) }}
{% endmacro %}

{#- 0006: additive attribution for maintenance outcomes. Each row records the trigger marker of
    the invocation that ran maintenance (read once before any step) and whether it ran
    automatically or manually, so dqm_maintenance_health can order outcomes after raw history is
    pruned. Existing rows keep null attribution and are assessed conservatively. #}
{% macro default__maintenance_attribution_apply() %}
  alter table {{ dbt_dqm.dqm_relation('dqm_maintenance_log') }} add column if not exists trigger_marker_at {{ dbt.type_timestamp() }};
  alter table {{ dbt_dqm.dqm_relation('dqm_maintenance_log') }} add column if not exists trigger_kind {{ dbt.type_string() }};
{% endmacro %}
{% macro default__maintenance_attribution_verify() %}
  {% set timestamp_types = "('timestamp','timestamp without time zone')" %}
  {% set string_types = "('string','character varying','text')" %}
  {{ dbt_dqm.assert_sql('(select count(*) from ' ~ dbt_dqm.columns_catalog() ~ ' where table_schema=' ~ dbt_dqm.sql_string(dbt_dqm.dqm_relation('dqm_maintenance_log').schema)
      ~ " and table_name=" ~ dbt_dqm.sql_string(dbt_dqm.dqm_relation('dqm_maintenance_log').identifier) ~ " and ((lower(column_name)='trigger_marker_at' and lower(data_type) in " ~ timestamp_types
      ~ ") or (lower(column_name)='trigger_kind' and lower(data_type) in " ~ string_types ~ '))) = 2',
    dbt_dqm.sql_string('Maintenance attribution columns are missing or have the wrong type. Rerun setup with the current package.')) }}
{% endmacro %}

{#- 0007: warehouse browsing reads Active issues in the review app's sort order (priority rank,
    first seen, occurrence id) one keyset page at a time. On Postgres a
    partial expression index on exactly those keys turns each page into a short index range scan
    instead of sorting every Active issue (measured at 1M occurrences / 100k Active: first page
    ~0.5 s to ~0.05 s). The expressions must match the app's (src/dbt_dqm_app/browse.py) character
    for character; tests/test_browse.py pins that. BigQuery has no indexes, and changing an existing
    table's clustering isn't available in SQL, so this migration only records the version there;
    BigQuery page costs are documented in docs/scale.md. #}
{% macro browse_priority_rank_sql() %}(case lower(coalesce(test_priority, '')) when 'critical' then 0 when 'high' then 1 when 'medium' then 2 when 'low' then 3 else 4 end){% endmacro %}
{% macro default__browse_keyset_index_apply() %}
  {% if target.type == 'postgres' %}
    create index if not exists {{ adapter.quote(dbt_dqm.index_name('dqm_issue_occurrences','browse_active')) }} on {{ dbt_dqm.dqm_relation('dqm_issue_occurrences') }}
      (({{ dbt_dqm.browse_priority_rank_sql() }}), first_seen_at, occurrence_id)
      where record_status = 'Active';
  {% endif %}
{% endmacro %}
{% macro default__browse_keyset_index_verify() %}
  {% if target.type == 'postgres' %}
    {{ dbt_dqm.assert_sql("exists(select 1 from pg_indexes where schemaname=" ~ dbt_dqm.sql_string(dbt_dqm.dqm_relation('dqm_issue_occurrences').schema) ~ " and indexname=" ~ dbt_dqm.sql_string(dbt_dqm.index_name('dqm_issue_occurrences','browse_active')) ~ ")",
      dbt_dqm.sql_string('The warehouse browsing index is missing. Rerun setup with the current package.')) }}
  {% endif %}
{% endmacro %}

{#- Event payload retention: full before/after values (default), only changed column names plus
    digests, or none. Digests are not anonymization: predictable values can be guessed. #}
{% macro event_payload_mode() %}
  {% set mode = var('dbt_dqm_event_payloads', 'full') %}
  {% if mode not in ['full', 'changed_columns', 'none'] %}
    {{ exceptions.raise_compiler_error('dbt_dqm_event_payloads must be full, changed_columns or none.') }}
  {% endif %}
  {{ return(mode) }}
{% endmacro %}

{% macro columns_catalog() %}
  {% set r = dbt_dqm.dqm_relation('dqm_install') %}
  {% if target.type == 'bigquery' %}`{{ r.database }}.{{ r.schema }}.INFORMATION_SCHEMA.COLUMNS`
  {% else %}information_schema.columns{% endif %}
{% endmacro %}
{% macro tables_catalog() %}
  {% set r = dbt_dqm.dqm_relation('dqm_install') %}
  {% if target.type == 'bigquery' %}`{{ r.database }}.{{ r.schema }}.INFORMATION_SCHEMA.TABLES`
  {% else %}information_schema.tables{% endif %}
{% endmacro %}
{% macro table_exists_sql(name) %}
  exists(select 1 from {{ dbt_dqm.tables_catalog() }} where table_schema={{ dbt_dqm.sql_string(dbt_dqm.dqm_relation(name).schema) }} and table_name={{ dbt_dqm.sql_string(dbt_dqm.dqm_relation(name).identifier) }})
{% endmacro %}
{% macro default__initial_schema_verify() %}
  {% for name, columns in dbt_dqm.table_schemas().items() if name not in ['dqm_annotation_changes','dqm_missed_issues'] %}
    {% set names = [] %}{% for col, kind in columns %}{% do names.append(dbt_dqm.sql_string(col)) %}{% endfor %}
    {{ dbt_dqm.assert_sql('(select count(*) from ' ~ dbt_dqm.columns_catalog() ~ ' where table_schema=' ~ dbt_dqm.sql_string(dbt_dqm.dqm_relation(name).schema) ~ ' and table_name=' ~ dbt_dqm.sql_string(dbt_dqm.dqm_relation(name).identifier) ~ ' and lower(column_name) in (' ~ names|join(',') ~ ')) = ' ~ columns|length,
      dbt_dqm.sql_string('Incomplete DQM schema: ' ~ name ~ '. Rerun setup with the current package.')) }}
  {% endfor %}
{% endmacro %}

{% macro latest_migration() %}{{ return(dbt_dqm.migrations()[-1]['id']) }}{% endmacro %}
{% macro ready_sql() %}
  {{ dbt_dqm.assert_sql('(select count(*) from ' ~ dbt_dqm.dqm_relation('dqm_reconciliation_control') ~ " where setup_status='ready' and schema_version=" ~ dbt_dqm.sql_string(dbt_dqm.latest_migration()) ~ ')=1',
      dbt_dqm.sql_string('DQM setup is incomplete or package versions differ. Retry setup with the current version.')) }}
{% endmacro %}

{#
  Installation and migrations, run before capture and reconciliation:

    1. refuse a recognizable 0.1 schema (before writing anything)
    2. create the 0.2 install marker, the singleton control row and the migration ledger
    3. apply pending migrations in registry order (apply -> backfill -> verify -> record)
    4. require setup to be ready at the latest migration
    5. grant tracking tables (only from reconciliation and capture; see below)

  Postgres runs this under the transaction-scoped advisory lock, so migrations are serialized
  and an interrupted one rolls back. BigQuery can't run DDL in transactions, so a migration owner
  claims a lease in the control row and every later step re-checks it (bigquery_migration_*).
#}
{% macro setup_sql(grants=true) %}
  {#- Aliased: dbt's static parser rejects adapter.dispatch with a non-literal macro name. #}
  {% set migration_dispatch = adapter.dispatch %}
  {% if not execute or dbt_dqm.empty_mode() %}{{ return('') }}{% endif %}
  {% set install = dbt_dqm.dqm_relation('dqm_install') %}
  {% set control = dbt_dqm.dqm_relation('dqm_reconciliation_control') %}
  {% set ledger = dbt_dqm.dqm_relation('dqm_schema_migrations') %}
  {% set installed = adapter.get_relation(database=install.database,schema=install.schema,identifier=install.identifier) if flags.INTROSPECT|default(true) else none %}
  {% if installed %}
    {% set marker = run_query('select marker from ' ~ install) %}
    {% if marker.rows|length != 1 or marker.rows[0][0] != 'dqm-full-v1' %}
      {{ exceptions.raise_compiler_error('Incompatible DQM schema. Run dbt-dqm run --full-refresh. Existing history is not migrated.') }}
    {% endif %}
    {% set state = run_query('select setup_status,schema_version from ' ~ control) %}
    {% if state.rows|length == 1 and state.rows[0][0]=='ready' and state.rows[0][1]==dbt_dqm.latest_migration() %}
      {% set ready %}{{ dbt_dqm.ready_sql() }}
        {% for name, table_grant in (var('dbt_dqm_table_grants',{}).items() if grants else []) %}
          {% if name not in ['dqm_annotation_changes','dqm_missed_issues'] %}{{ dbt_dqm.table_grants_sql(name,table_grant) }}{% endif %}
        {% endfor %}
      {% endset %}
      {{ return(ready) }}
    {% endif %}
  {% endif %}
  {% set lease = dbt_dqm.positive_integer_var('dbt_dqm_reconcile_timeout_minutes', 60) %}
  {% set legacy = ['dqm_test_executions','dqm_issue_observations','dqm_issue_occurrences','dqm_reconciliation_state','dqm_schema_migrations','dqm_annotation_changes'] %}
  {% set legacy_checks = [] %}
  {% for name in legacy %}{% do legacy_checks.append(dbt_dqm.table_exists_sql(name)) %}{% endfor %}
  {% if target.type == 'bigquery' %}
    declare dqm_migration_token string default generate_uuid();
    declare dqm_needs_setup bool;
  {% endif %}

  {% if install.identifier != 'dqm_install' %}
    {% set legacy_install = "exists(select 1 from " ~ dbt_dqm.tables_catalog() ~ " where table_schema=" ~ dbt_dqm.sql_string(install.schema) ~ " and table_name in ('dqm_install','dqm_issue_occurrences','dqm_test_executions','dqm_issue_events','dqm_issue_observations','dqm_reconciliation_control','dqm_schema_migrations'))" %}
    {{ dbt_dqm.assert_sql('(' ~ dbt_dqm.table_exists_sql('dqm_install') ~ ') or not (' ~ legacy_install ~ ')',
      dbt_dqm.sql_string('DQM naming now isolates state tables at ' ~ install ~ '. Legacy shared dqm_install/history remain untouched. Start this isolated installation explicitly with dbt-dqm run --full-refresh and confirm its scope (--yes for unattended use). History is not copied.')) }}
  {% endif %}
  {#- 1-2. A 0.1 schema has tracking tables but no install marker. #}
  {{ dbt_dqm.assert_sql(
    '(' ~ dbt_dqm.table_exists_sql('dqm_install') ~ ') or not (' ~ legacy_checks | join(' or ') ~ ')',
    dbt_dqm.sql_string('dbt-dqm 0.2 requires a fresh DQM schema. Set dbt_dqm_schema to a new schema; run dbt-dqm run --full-refresh; existing history is not migrated.')) }}
  create table if not exists {{ install }} as select 'dqm-full-v1' as marker, {{ dbt.current_timestamp() }} as created_at, {{ dbt_dqm.sql_string(invocation_id) }} as installation_id, cast(null as {{ dbt.type_string() }}) as pending_view_grants;
  create table if not exists {{ control }} as select cast(0 as {{ dbt.type_bigint() }}) as generation,
    'ready' as setup_status, cast(null as {{ dbt.type_string() }}) as migration_owner,
    cast(null as {{ dbt.type_timestamp() }}) as migration_lease_until, cast(null as {{ dbt.type_string() }}) as schema_version, cast(null as {{ dbt.type_timestamp() }}) as raw_pruned_before;
  create table if not exists {{ ledger }} (migration_id {{ dbt.type_string() }} not null {% if target.type == 'postgres' %}primary key{% endif %}, applied_at {{ dbt.type_timestamp() }});
  {{ dbt_dqm.assert_sql(
    '(select count(*) from ' ~ install ~ ")=1 and (select marker from " ~ install ~ ")='dqm-full-v1' and (select count(*) from " ~ control ~ ')=1',
    dbt_dqm.sql_string('Incompatible DQM schema. Run dbt-dqm run --full-refresh.')) }}
  {{ dbt_dqm.assert_sql(
    'not exists(select 1 from ' ~ control ~ ' where schema_version > ' ~ dbt_dqm.sql_string(dbt_dqm.latest_migration()) ~ ')',
    dbt_dqm.sql_string('DQM schema is newer than this package. Upgrade the package.')) }}

  {#- 3. Migrations. Each bumps the generation, which fences reconciliations frozen before it. #}
  {% if target.type == 'postgres' %}
    {% for spec in dbt_dqm.migrations() %}
      do $dqm_migration$ begin if not exists(select 1 from {{ ledger }} where migration_id={{ dbt_dqm.sql_string(spec.id) }}) then
        update {{ control }} set setup_status='migrating', generation=generation+1 where true;
        {{ migration_dispatch(spec.apply, 'dbt_dqm')() }}
        {% if spec.get('backfill') %}{{ migration_dispatch(spec.backfill, 'dbt_dqm')() }}{% endif %}
        {{ migration_dispatch(spec.verify, 'dbt_dqm')() }}
        insert into {{ ledger }} values ({{ dbt_dqm.sql_string(spec.id) }}, {{ dbt.current_timestamp() }});
        update {{ control }} set setup_status='ready', schema_version={{ dbt_dqm.sql_string(spec.id) }}, generation=generation+1 where true;
      end if; end $dqm_migration$;
    {% endfor %}
  {% elif target.type == 'bigquery' %}
    {% set migration_ids = [] %}
    {% for spec in dbt_dqm.migrations() %}{% do migration_ids.append(dbt_dqm.sql_string(spec.id)) %}{% endfor %}
    set dqm_needs_setup = (select count(*) from {{ ledger }} where migration_id in ({{ migration_ids | join(',') }})) < {{ migration_ids | length }};
    if dqm_needs_setup then
      {#- Claim the migration lease; refuse while another owner's lease is live. #}
      begin transaction;
      {{ dbt_dqm.assert_sql(
        'not exists(select 1 from ' ~ control ~ " where setup_status='migrating' and migration_lease_until > current_timestamp())",
        dbt_dqm.sql_string('DQM migration is already running. Retry after it completes or its lease expires.')) }}
      update {{ control }} set setup_status='migrating', migration_owner=dqm_migration_token,
        migration_lease_until=timestamp_add(current_timestamp(), interval {{ lease }} minute), generation=generation+1 where true;
      commit transaction;
      {% for spec in dbt_dqm.migrations() %}
        if not exists(select 1 from {{ ledger }} where migration_id={{ dbt_dqm.sql_string(spec.id) }}) then
          {{ dbt_dqm.bigquery_migration_apply(spec, 'dqm_migration_token') }}
          {{ dbt_dqm.bigquery_migration_commit(spec, 'dqm_migration_token') }}
        end if;
      {% endfor %}
      {{ dbt_dqm.bigquery_migration_release('dqm_migration_token') }}
    end if;
  {% endif %}

  {#- 4. #}
  {{ dbt_dqm.ready_sql() }}

  {#- 5. Only reconciliation and capture grant tracking tables. The app-table models run setup in
      parallel threads, and concurrent BigQuery GRANTs on one table fail with an IAM ETag
      conflict ("concurrent policy changes"). Each app table grants itself in app_table_finish.
      The control and app staging tables are created by setup rather than table_schemas(), but
      the review app reads control to verify syncs and writes staging rows, so they're grantable. #}
  {% for name, table_grant in (var('dbt_dqm_table_grants', {}).items() if grants else []) %}
    {% if name not in dbt_dqm.table_schemas() and name not in ['dqm_install', 'dqm_reconciliation_control', 'dqm_app_change_staging', 'dqm_maintenance_log'] %}
      {{ exceptions.raise_compiler_error('Unknown DQM grant table: ' ~ name) }}
    {% endif %}
    {% if name not in ['dqm_annotation_changes','dqm_missed_issues'] %}{{ dbt_dqm.table_grants_sql(name, table_grant) }}{% endif %}
  {% endfor %}
{% endmacro %}

{% macro ensure_capture_tables() %}
  {% if execute and not dbt_dqm.empty_mode() %}
    {% set sql %}{% if target.type=='postgres' %}begin; {{ dbt_dqm.reconcile_lock() }}{% endif %}
      {{ dbt_dqm.setup_sql() }}{% if target.type=='postgres' %}commit;{% endif %}{% endset %}
    {% do run_query(sql) %}
  {% endif %}
{% endmacro %}

{% macro app_table_setup() %}
  {% if not execute or dbt_dqm.empty_mode() %}{{ return('') }}{% endif %}
  {{ dbt_dqm.reconcile_lock() }} {{ dbt_dqm.setup_sql(grants=false) }}
{% endmacro %}

{% macro empty_preflight() %}
  {% if execute and var('dqm_enabled',false) and dbt_dqm.empty_mode() %}
    {% set r=dbt_dqm.dqm_relation('dqm_reconciliation_control') %}
    {% if adapter.get_relation(database=r.database,schema=r.schema,identifier=r.identifier) is none %}
      {{ exceptions.raise_compiler_error('--empty requires an initialized dbt-dqm 0.2 schema. Run a normal package build first.') }}
    {% endif %}
    {{ dbt_dqm.ready_sql() }}
    {{ dbt_dqm.default__initial_schema_verify() }}
  {% endif %}
{% endmacro %}

{% macro ensure_column(relation,column_name,column_type) %}
  alter table {{ relation }} add column if not exists {{ adapter.quote(column_name) }} {{ column_type }};
{% endmacro %}

{% macro native_grants_sql(relation,grants,resource_type='TABLE') %}
  {% if grants is not mapping %}{{ exceptions.raise_compiler_error('Grants must map native privileges to principal lists.') }}{% endif %}
  {% for privilege, grantees in grants.items() %}
    {% if grantees is string or grantees is not sequence %}{{ exceptions.raise_compiler_error('Grant principals must be a list.') }}{% endif %}
    {% if grantees|length > 0 %}
      {% if target.type=='bigquery' %}
        grant {{ adapter.quote(privilege) }} on {{ resource_type }} {{ relation }} to
        {% for principal in grantees %}{{ dbt_dqm.sql_string(principal) }}{% if not loop.last %}, {% endif %}{% endfor %};
      {% else %}
        {{ dbt.get_grant_sql(relation,privilege,grantees) }};
      {% endif %}
    {% endif %}
  {% endfor %}
{% endmacro %}
{% macro table_grants_sql(name,grants) %}
  {{ dbt_dqm.native_grants_sql(dbt_dqm.dqm_relation(name),grants) }}
{% endmacro %}

{% macro public_view_grants() %}
  {# dbt-bigquery's view materialization does not apply config.grants. #}
  {% if not execute or dbt_dqm.empty_mode() or target.type!='bigquery' or config.get('materialized')!='view' %}{{ return('') }}{% endif %}
  {{ dbt_dqm.native_grants_sql(this,config.get('grants',{}),'VIEW') }}
{% endmacro %}

{% macro app_table_finish(name) %}
  {% if not execute or dbt_dqm.empty_mode() %}{{ return('') }}{% endif %}
  {% if target.type=='postgres' %}
    {% set relation = dbt_dqm.dqm_relation(name) %}
    {# Keep canonical index names; aliases need their own bounded schema-wide index names. #}
    {% set index_prefix = name if relation.identifier == name else 'dqm_alias_' ~ local_md5(relation.identifier) %}
    create unique index if not exists {{ adapter.quote(index_prefix ~ '_primary') }} on {{ relation }} ({{ dbt_dqm.table_keys()[name]|join(',') }});
    {% if name=='dqm_annotation_changes' %}
      {% set history_index = 'dqm_annotation_history' if relation.identifier == name else index_prefix ~ '_history' %}
      create index if not exists {{ adapter.quote(history_index) }} on {{ relation }} (occurrence_id,changed_at);
    {% endif %}
  {% endif %}
  {{ dbt_dqm.table_grants_sql(name,var('dbt_dqm_table_grants',{}).get(name,{})) }}
{% endmacro %}

{#- BigQuery migration steps, each fenced by the owner token. Permanent DDL can't run inside a
    transaction, so a migration owner can stall between steps and lose its lease to another
    owner. Every write after the DDL re-checks ownership inside a transaction, so a resumed stale
    owner can't backfill, record completion or release the new owner's lease. `token` is a SQL
    expression (the script variable in setup_sql, or a literal in tests). #}
{% macro bigquery_migration_guard(token) %}
  {% set control = dbt_dqm.dqm_relation('dqm_reconciliation_control') %}
  {{ dbt_dqm.assert_sql('(select migration_owner from ' ~ control ~ ')=' ~ token ~ ' and (select migration_lease_until from ' ~ control ~ ') > current_timestamp()', dbt_dqm.sql_string('Stale DQM migration owner; retry.')) }}
{% endmacro %}

{% macro bigquery_migration_apply(spec, token) %}
  {#- Aliased: dbt's static parser rejects adapter.dispatch with a non-literal macro name. #}
  {% set migration_dispatch = adapter.dispatch %}
  {{ dbt_dqm.bigquery_migration_guard(token) }}
  {{ migration_dispatch(spec.apply, 'dbt_dqm')() }}
{% endmacro %}

{% macro bigquery_migration_commit(spec, token) %}
  {% set control = dbt_dqm.dqm_relation('dqm_reconciliation_control') %}
  {% set lease = dbt_dqm.positive_integer_var('dbt_dqm_reconcile_timeout_minutes', 60) %}
  {% set migration_dispatch = adapter.dispatch %}
  begin transaction;
  {{ dbt_dqm.bigquery_migration_guard(token) }}
  {% if spec.get('backfill') %}{{ migration_dispatch(spec.backfill, 'dbt_dqm')() }}{% endif %}
  {{ migration_dispatch(spec.verify, 'dbt_dqm')() }}
  insert into {{ dbt_dqm.dqm_relation('dqm_schema_migrations') }} values ({{ dbt_dqm.sql_string(spec.id) }}, current_timestamp());
  update {{ control }} set migration_lease_until=timestamp_add(current_timestamp(), interval {{ lease }} minute) where true;
  commit transaction;
{% endmacro %}

{% macro bigquery_migration_release(token) %}
  {% set control = dbt_dqm.dqm_relation('dqm_reconciliation_control') %}
  begin transaction;
  {{ dbt_dqm.bigquery_migration_guard(token) }}
  update {{ control }} set setup_status='ready', schema_version={{ dbt_dqm.sql_string(dbt_dqm.latest_migration()) }},
    migration_owner=null, migration_lease_until=null, generation=generation+1 where true;
  commit transaction;
{% endmacro %}
