-- depends_on: {{ ref('dqm_annotation_changes') }}
{{ config(enabled=var('dqm_enabled',false), schema=var('dbt_dqm_schema', none), materialized='dqm_app_table' if target.type == 'bigquery' else 'incremental',incremental_strategy=dbt_dqm.incremental_upsert_strategy(),
    unique_key='missed_issue_id',full_refresh=false,on_schema_change='append_new_columns') }}
select {% for col,kind in dbt_dqm.table_schemas()['dqm_missed_issues'] %}
  cast(null as {{ context['dbt']['type_' ~ kind]() }}) as {{ col }}{% if not loop.last %},{% endif %}
{% endfor %}
from {{ dbt_dqm.dual() }} where false
