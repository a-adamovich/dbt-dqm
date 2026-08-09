-- Field-level audit ledger populated by idempotent local-app apply batches.
-- The dbt model owns the empty table schema; the application appends immutable change events.
{{
  config(
    materialized='incremental',
    unique_key=['batch_id', 'occurrence_id', 'field_name'],
    full_refresh=false
  )
}}

select
  cast(null as string) as batch_id,
  cast(null as string) as occurrence_id,
  cast(null as string) as field_name,
  cast(null as string) as old_value,
  cast(null as string) as new_value,
  cast(null as timestamp) as changed_at,
  cast(null as string) as changed_by
from unnest([1])
where false
