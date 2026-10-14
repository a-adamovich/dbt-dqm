{# The reset contract names only package-owned relations, never consumer failure tables. #}
{% macro refresh_contract() %}
  {% set schemas=dbt_dqm.table_schemas().copy() %}
  {% do schemas.update({
    'dqm_install':[('marker','string'),('created_at','timestamp'),('installation_id','string'),('pending_view_grants','string')],
    'dqm_reconciliation_control':[('generation','bigint'),('setup_status','string'),('migration_owner','string'),('migration_lease_until','timestamp'),('schema_version','string'),('raw_pruned_before','timestamp')],
    'dqm_app_change_staging':[('batch_id','string'),('occurrence_id','string'),('field_name','string'),('old_value','string'),('new_value','string'),('changed_at','timestamp'),('changed_by','string'),('base_annotation_version','int'),('upload_id','string'),('staged_at','timestamp'),('installation_id','string')],
    'dqm_maintenance_log':[('logged_at','timestamp'),('invocation_id','string'),('step','string'),('outcome','string'),('description','string'),('diagnostic_id','string'),('trigger_marker_at','timestamp'),('trigger_kind','string')]
  }) %}
  {% set tables={} %}
  {% for name,columns in schemas.items() %}
    {% if target.type=='postgres' and name=='dqm_app_change_staging' %}{% continue %}{% endif %}
    {% set relation=dbt_dqm.dqm_relation(name) %}
    {% do tables.update({name:{'database':relation.database,'schema':relation.schema,'identifier':relation.identifier,'columns':columns,'keys':dbt_dqm.table_keys().get(name,[])}}) %}
  {% endfor %}
  {{ return(tables) }}
{% endmacro %}
