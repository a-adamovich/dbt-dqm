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
    select pg_advisory_xact_lock(hashtext({{ dbt_dqm.sql_string('dbt_dqm:' ~ dbt_dqm.dqm_relation('dqm_install').schema) }}));
  {% endif %}
{% endmacro %}

{% macro migrations() %}{{ return(adapter.dispatch('migrations', 'dbt_dqm')()) }}{% endmacro %}
{% macro default__migrations() %}
  {{ return([{'id': '0001_initial', 'description': 'Fresh 0.2 schema', 'apply': 'initial_schema_apply', 'verify': 'initial_schema_verify'}]) }}
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
    create index if not exists dqm_occurrence_identity_history on {{ dbt_dqm.dqm_relation('dqm_issue_occurrences') }} (test_unique_id, unique_id, occurrence_number);
    create index if not exists dqm_occurrence_active_identity on {{ dbt_dqm.dqm_relation('dqm_issue_occurrences') }} (test_unique_id, unique_id) where record_status='Active';
    create index if not exists dqm_execution_order on {{ dbt_dqm.dqm_relation('dqm_test_executions') }} (test_unique_id, captured_at, invocation_id);
    create index if not exists dqm_observation_identity on {{ dbt_dqm.dqm_relation('dqm_issue_observations') }} (test_unique_id, unique_id, invocation_id);

  {% endif %}
{% endmacro %}
{% macro default__initial_schema_apply() %}{{ dbt_dqm.capture_tables_ddl() }}{% endmacro %}

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
  exists(select 1 from {{ dbt_dqm.tables_catalog() }} where table_schema={{ dbt_dqm.sql_string(dbt_dqm.dqm_relation(name).schema) }} and table_name={{ dbt_dqm.sql_string(name) }})
{% endmacro %}
{% macro default__initial_schema_verify() %}
  {% for name, columns in dbt_dqm.table_schemas().items() if name not in ['dqm_annotation_changes','dqm_missed_issues'] %}
    {% set names = [] %}{% for col, kind in columns %}{% do names.append(dbt_dqm.sql_string(col)) %}{% endfor %}
    {{ dbt_dqm.assert_sql('(select count(*) from ' ~ dbt_dqm.columns_catalog() ~ ' where table_schema=' ~ dbt_dqm.sql_string(dbt_dqm.dqm_relation(name).schema) ~ ' and table_name=' ~ dbt_dqm.sql_string(name) ~ ' and lower(column_name) in (' ~ names|join(',') ~ ')) = ' ~ columns|length,
      dbt_dqm.sql_string('Incomplete DQM schema: ' ~ name ~ '. Rerun setup with the current package.')) }}
  {% endfor %}
{% endmacro %}

{% macro latest_migration() %}{{ return(dbt_dqm.migrations()[-1]['id']) }}{% endmacro %}
{% macro ready_sql() %}
  {{ dbt_dqm.assert_sql('(select count(*) from ' ~ dbt_dqm.dqm_relation('dqm_reconciliation_control') ~ " where setup_status='ready' and schema_version=" ~ dbt_dqm.sql_string(dbt_dqm.latest_migration()) ~ ')=1',
      dbt_dqm.sql_string('DQM setup is incomplete or package versions differ. Retry setup with the current version.')) }}
{% endmacro %}

{% macro setup_sql() %}
  {% set migration_dispatch=adapter.dispatch %}
  {% if not execute or dbt_dqm.empty_mode() %}{{ return('') }}{% endif %}
  {% set install=dbt_dqm.dqm_relation('dqm_install') %}
  {% set control=dbt_dqm.dqm_relation('dqm_reconciliation_control') %}
  {% set ledger=dbt_dqm.dqm_relation('dqm_schema_migrations') %}
  {% set lease=dbt_dqm.positive_integer_var('dbt_dqm_reconcile_timeout_minutes',60) %}
  {% set legacy=['dqm_test_executions','dqm_issue_observations','dqm_issue_occurrences','dqm_reconciliation_state','dqm_schema_migrations','dqm_annotation_changes'] %}
  {% set legacy_checks=[] %}{% for name in legacy %}{% do legacy_checks.append(dbt_dqm.table_exists_sql(name)) %}{% endfor %}
  {% if target.type == 'bigquery' %}
    declare dqm_migration_token string default generate_uuid();
    declare dqm_needs_setup bool;
  {% endif %}
  {{ dbt_dqm.assert_sql('(' ~ dbt_dqm.table_exists_sql('dqm_install') ~ ') or not (' ~ legacy_checks|join(' or ') ~ ')',
    dbt_dqm.sql_string('dbt-dqm 0.2 requires a fresh DQM schema. Set dbt_dqm_schema to a new schema; existing 0.1 history is not migrated.')) }}
  create table if not exists {{ install }} as select 'dqm-0.2' as marker, {{ dbt.current_timestamp() }} as created_at;
  create table if not exists {{ control }} as select cast(0 as {{ dbt.type_bigint() }}) as generation,
    'ready' as setup_status, cast(null as {{ dbt.type_string() }}) as migration_owner,
    cast(null as {{ dbt.type_timestamp() }}) as migration_lease_until, cast(null as {{ dbt.type_string() }}) as schema_version, cast(null as {{ dbt.type_timestamp() }}) as raw_pruned_before;
  create table if not exists {{ ledger }} (migration_id {{ dbt.type_string() }} not null {% if target.type=='postgres' %}primary key{% endif %}, applied_at {{ dbt.type_timestamp() }});
  {{ dbt_dqm.assert_sql('(select count(*) from ' ~ install ~ ")=1 and (select marker from " ~ install ~ ")='dqm-0.2' and (select count(*) from " ~ control ~ ')=1', dbt_dqm.sql_string('Invalid DQM install marker or singleton control table.')) }}
  {{ dbt_dqm.assert_sql('not exists(select 1 from ' ~ control ~ ' where schema_version > ' ~ dbt_dqm.sql_string(dbt_dqm.latest_migration()) ~ ')', dbt_dqm.sql_string('DQM schema is newer than this package. Upgrade the package.')) }}
  {% if target.type=='postgres' %}
    {% for spec in dbt_dqm.migrations() %}
      do $dqm_migration$ begin if not exists(select 1 from {{ ledger }} where migration_id={{ dbt_dqm.sql_string(spec.id) }}) then
        update {{ control }} set setup_status='migrating', generation=generation+1;
        {{ migration_dispatch(spec.apply, 'dbt_dqm')() }}
        {% if spec.get('backfill') %}{{ migration_dispatch(spec.backfill,'dbt_dqm')() }}{% endif %}
        {{ migration_dispatch(spec.verify, 'dbt_dqm')() }}
        insert into {{ ledger }} values ({{ dbt_dqm.sql_string(spec.id) }}, {{ dbt.current_timestamp() }});
        update {{ control }} set setup_status='ready', schema_version={{ dbt_dqm.sql_string(spec.id) }}, generation=generation+1;
      end if; end $dqm_migration$;
    {% endfor %}
  {% elif target.type=='bigquery' %}
    set dqm_needs_setup = (select count(*) from {{ ledger }} where migration_id in ({% for spec in dbt_dqm.migrations() %}{{ dbt_dqm.sql_string(spec.id) }}{% if not loop.last %},{% endif %}{% endfor %})) < {{ dbt_dqm.migrations()|length }};
    if dqm_needs_setup then
      begin transaction;
      {{ dbt_dqm.assert_sql('not exists(select 1 from ' ~ control ~ " where setup_status='migrating' and migration_lease_until > current_timestamp())", dbt_dqm.sql_string('DQM migration is already running. Retry after it completes or its lease expires.')) }}
      update {{ control }} set setup_status='migrating', migration_owner=dqm_migration_token,
        migration_lease_until=timestamp_add(current_timestamp(), interval {{ lease }} minute), generation=generation+1;
      commit transaction;
      {% for spec in dbt_dqm.migrations() %}
        if not exists(select 1 from {{ ledger }} where migration_id={{ dbt_dqm.sql_string(spec.id) }}) then
          {{ dbt_dqm.assert_sql('(select migration_owner from ' ~ control ~ ')=dqm_migration_token and (select migration_lease_until from ' ~ control ~ ') > current_timestamp()', dbt_dqm.sql_string('Stale DQM migration owner; retry.')) }}
          {{ migration_dispatch(spec.apply, 'dbt_dqm')() }}
          begin transaction;
          {{ dbt_dqm.assert_sql('(select migration_owner from ' ~ control ~ ')=dqm_migration_token and (select migration_lease_until from ' ~ control ~ ') > current_timestamp()', dbt_dqm.sql_string('Stale DQM migration owner; retry.')) }}
          {% if spec.get('backfill') %}{{ migration_dispatch(spec.backfill,'dbt_dqm')() }}{% endif %}
          {{ migration_dispatch(spec.verify, 'dbt_dqm')() }}
          insert into {{ ledger }} values ({{ dbt_dqm.sql_string(spec.id) }}, current_timestamp());
          update {{ control }} set migration_lease_until=timestamp_add(current_timestamp(), interval {{ lease }} minute);
          commit transaction;
        end if;
      {% endfor %}
      begin transaction;
      {{ dbt_dqm.assert_sql('(select migration_owner from ' ~ control ~ ')=dqm_migration_token and (select migration_lease_until from ' ~ control ~ ') > current_timestamp()', dbt_dqm.sql_string('Stale DQM migration owner; retry.')) }}
      update {{ control }} set setup_status='ready', schema_version={{ dbt_dqm.sql_string(dbt_dqm.latest_migration()) }},
        migration_owner=null, migration_lease_until=null, generation=generation+1;
      commit transaction;
    end if;
  {% endif %}
  {{ dbt_dqm.ready_sql() }}
  {% for name, grants in var('dbt_dqm_table_grants', {}).items() %}
    {% if name not in dbt_dqm.table_schemas() %}{{ exceptions.raise_compiler_error('Unknown DQM grant table: ' ~ name) }}{% endif %}
    {% if name not in ['dqm_annotation_changes','dqm_missed_issues'] %}{{ dbt_dqm.table_grants_sql(name, grants) }}{% endif %}
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
  {{ dbt_dqm.reconcile_lock() }} {{ dbt_dqm.setup_sql() }}
{% endmacro %}

{% macro empty_preflight() %}
  {% if execute and dbt_dqm.empty_mode() %}
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

{% macro table_grants_sql(name,grants) %}
  {% if grants is not mapping %}{{ exceptions.raise_compiler_error('Grants must map native privileges to principal lists.') }}{% endif %}
  {% for privilege, grantees in grants.items() %}
    {% if grantees is string or grantees is not sequence %}{{ exceptions.raise_compiler_error('Grant principals must be a list.') }}{% endif %}
    {{ dbt.get_grant_sql(dbt_dqm.dqm_relation(name),privilege,grantees) }};
  {% endfor %}
{% endmacro %}
{% macro app_table_finish(name) %}
  {% if not execute or dbt_dqm.empty_mode() %}{{ return('') }}{% endif %}
  {% if target.type=='postgres' %}
    create unique index if not exists {{ name }}_primary on {{ dbt_dqm.dqm_relation(name) }} ({{ dbt_dqm.table_keys()[name]|join(',') }});
    {% if name=='dqm_annotation_changes' %}create index if not exists dqm_annotation_history on {{ dbt_dqm.dqm_relation(name) }} (occurrence_id,changed_at);{% endif %}
  {% endif %}
  {{ dbt_dqm.table_grants_sql(name,var('dbt_dqm_table_grants',{}).get(name,{})) }}
{% endmacro %}
