{#
  Postgres entry view for dqm_reconcile.

  dbt's default view materialization drops cached `__dbt_tmp`/`__dbt_backup` relations and later
  renames the existing view, using relation-cache state read when the run started. It does the
  drops *before* the transactional pre-hooks, so before reconcile_lock takes the advisory lock.
  Two overlapping reconciliations could therefore act on each other's temporary relations, and a
  cache entry for a relation that disappeared in between could render as `drop external` (#17).

  This materialization keeps the default's order of operations but replaces the view in place
  inside the locked transaction, with no temporary or backup relations and no DDL before the lock:

    pre-hooks (outside, then inside the transaction: reconcile_lock, reconcile_pre)
    -> create or replace view
    -> grants, persisted docs
    -> transactional post-hooks (apply_reconciliation)
    -> commit -> post-hooks outside the transaction

  Constraint: Postgres CREATE OR REPLACE VIEW keeps the existing columns' names, order and types
  and can only append new ones. The entry view is `select *` over dqm_issue_occurrences, which
  only changes through additive migrations, so replacement stays valid.

  BigQuery keeps its adapter's view materialization (models/dqm_reconcile.sql picks per adapter):
  it already replaces views atomically without temporary relations.
#}
{% materialization dqm_entry_view, adapter='postgres' %}
  {%- set existing_relation = load_cached_relation(this) -%}
  {%- set target_relation = this.incorporate(type='view') -%}
  {%- if existing_relation is not none and not existing_relation.is_view -%}
    {{ exceptions.raise_compiler_error(
      'dbt-dqm expected ' ~ this ~ ' to be a view but found a ' ~ existing_relation.type
      ~ '. It will not be dropped automatically; rename or drop it, then rerun.') }}
  {%- endif -%}
  {% set grant_config = config.get('grants') %}

  {{ run_hooks(pre_hooks, inside_transaction=False) }}
  -- `BEGIN` happens here; reconcile_lock is the first transactional pre-hook.
  {{ run_hooks(pre_hooks, inside_transaction=True) }}

  {% call statement('main') -%}
    create or replace view {{ target_relation }} as (
      {{ sql }}
    );
  {%- endcall %}

  {#- The view object (and its privileges) survives in place, so reconcile grants against the
      existing ones instead of treating the relation as new. #}
  {% set should_revoke = should_revoke(existing_relation, full_refresh_mode=False) %}
  {% do apply_grants(target_relation, grant_config, should_revoke=should_revoke) %}
  {% do persist_docs(target_relation, model) %}

  {{ run_hooks(post_hooks, inside_transaction=True) }}
  {{ adapter.commit() }}
  {{ run_hooks(post_hooks, inside_transaction=False) }}

  {#- dbt registers returned relations in its relation cache. #}
  {{ return({'relations': [target_relation]}) }}
{% endmaterialization %}

{# App-owned append-only tables have no model rows to merge. Own their creation so dataset
   defaults cannot change string semantics. Never replace a populated table on dbt reruns. #}
{% materialization dqm_app_table, adapter='bigquery' %}
  {% set existing = load_relation(this) %}
  {% set relation = this.incorporate(type='table') %}
  {% if existing is not none and not existing.is_table %}
    {{ exceptions.raise_compiler_error('Expected a DQM table at ' ~ this ~ '; no relation was replaced.') }}
  {% endif %}
  {{ run_hooks(pre_hooks) }}
  {% if dbt_dqm.empty_mode() %}
    {% call statement('main') %}select * from {{ relation }} where false{% endcall %}
  {% else %}
    {% call statement('main') %}
      create table if not exists {{ relation }} default collate '' as ({{ sql }});
    {% endcall %}
    {% set columns = adapter.get_columns_in_relation(relation) %}
    {% set names = columns|map(attribute='name')|map('lower')|list %}
    {% for column, kind in dbt_dqm.table_schemas()[model.name] if column|lower not in names %}
      {% call statement() %}
        {{ dbt_dqm.ensure_column(relation, column, context['dbt']['type_' ~ kind]()) }}
      {% endcall %}
    {% endfor %}
    {% do apply_grants(relation, config.get('grants'), should_revoke(existing, false)) %}
    {% do persist_docs(relation, model) %}
  {% endif %}
  {{ run_hooks(post_hooks) }}
  {{ return({'relations': [relation]}) }}
{% endmaterialization %}
