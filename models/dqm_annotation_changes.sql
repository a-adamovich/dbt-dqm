-- Field-level audit ledger populated by idempotent local-app apply batches.
-- The dbt model owns the empty table schema; the application appends immutable change events.
{{
  config(
    materialized='incremental',
    incremental_strategy=dbt_dqm.incremental_upsert_strategy(),
    unique_key=['batch_id', 'occurrence_id', 'field_name'],
    full_refresh=false,
    on_schema_change='append_new_columns',
    post_hook="{{ dbt_dqm.ensure_annotation_indexes() }}"
  )
}}

select
  cast(null as {{ dbt.type_string() }}) as batch_id,
  cast(null as {{ dbt.type_string() }}) as occurrence_id,
  cast(null as {{ dbt.type_string() }}) as field_name,
  cast(null as {{ dbt.type_string() }}) as old_value,
  cast(null as {{ dbt.type_string() }}) as new_value,
  cast(null as {{ dbt.type_timestamp() }}) as changed_at,
  cast(null as {{ dbt.type_string() }}) as changed_by,
  cast(null as {{ dbt.type_int() }}) as base_annotation_version,
  cast(null as {{ dbt.type_int() }}) as resulting_annotation_version
from {{ dbt_dqm.dual() }}
where false
