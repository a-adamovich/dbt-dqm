# Configuration reference

## Tracked data tests

In a singular test, use `config()` in the SQL. In a generic test, put the settings under `config:`.

```sql
{{ config(
    enabled=var('dqm_enabled', false),
    store_failures=true,
    limit=var('dbt_dqm_default_test_limit', 100),
    meta={'dbt_dqm': {
        'granularity': ['customer_id'],
        'owner': 'customer_data',
        'priority': 'high',
        'call_to_action': 'Correct the customer email address'
    }}
) }}

select customer_id, email
from {{ ref('customers') }}
where email is null
```

- The `meta.dbt_dqm` object opts in; tags are optional. The enable guard keeps the tracked test disabled in ordinary dbt runs. To keep a check in normal CI, retain its original test and give the DQM copy a separate name. See the [existing-project quickstart](quickstart-existing-project.md).
- Use `store_failures: true`, table failure storage, and dbt's `fail_calc: count(*)`. The native `limit` must be a positive integer. An omitted limit warns and uses the project default. Collection limits apply before deduplication. At the exact limit, captured rows can establish failures, but absence cannot establish a pass for omitted identities.
- Return the columns needed for a reviewer to identify and investigate each failed record. Capture includes all returned values. DQM validates the selected tests and their output before initialization or full refresh.

| `meta.dbt_dqm` key | Accepted value and effect |
| --- | --- |
| `granularity` | Required nonempty list of output column names. Names must be nonblank and unique after trimming and case normalization. Rows at the same grain form one issue observation. Changing the grain definition closes old identities structurally. |
| `owner` | String, list of strings, or null. Initializes the owner when no usable row owner exists. |
| `priority` | `critical`, `high`, `medium`, `low`, `unset`, or omitted. |
| `criticality` | Optional string describing the business effect. |
| `call_to_action` | Optional text suggesting a reviewer action. |

Owner precedence is the nonblank failure-row `dqm_owner` column, `meta.dbt_dqm.owner`, then `meta.owner`. Owner strings are trimmed, and nonblank list entries are joined with `, `. Empty values fall through to unassigned. Conflicting row owners at one grain fall back to metadata and produce a Health diagnostic. The chosen value initializes an occurrence. Reviewer assignments persist on later observations.

## Project variables

Set project defaults under `vars:` in `dbt_project.yml`. Use the same naming variables in `run`, `app`, and `workspace`.

| Variable | Default | Accepted value and effect |
| --- | --- | --- |
| `dqm_enabled` | `false` | Enables guarded nodes. The dedicated workflow enables them for its own execution. |
| `dbt_dqm_default_test_limit` | `100` | Positive integer fallback for omitted native test limits. |
| `dbt_dqm_retention_days` | Unset | Positive integer to prune processed raw evidence and old run-ledger rows. |
| `dbt_dqm_event_retention_days` | Unset | Positive integer to age out issue events. |
| `dbt_dqm_event_payloads` | `full` | `full`, `changed_columns`, or `none`. Controls values written to new events. Raw capture still includes all output values. |
| `dbt_dqm_reconcile_timeout_minutes` | `60` | Positive integer age for recovery of stale BigQuery reconciliation runs. |
| `dbt_dqm_metrics_window_days` | `30` | Positive integer reporting window for Health metrics. |
| `dbt_dqm_stale_days` | `14` | Positive integer age threshold for stale active issues. |
| `dbt_dqm_schema` | Unset | Optional custom schema name passed through dbt schema naming. |
| `dbt_dqm_table_grants` | `{}` | Warehouse table privileges mapped to lists of principals. See [grants](warehouse-interfaces.md#grants). |
| `dbt_dqm_maintenance_shortcuts` | `false` | Opts into experimental BigQuery maintenance probes where default macro behavior can be established. |
| `dbt_dqm_emit_still_failing_events` | `false` | Enables `STILL_FAILING` entries in the event ledger. |

Raw evidence and events are retained indefinitely unless retention is configured. Full event payloads may include personal data. `changed_columns` keeps names and digests. Use `none` to omit those fields as well. See [retention and maintenance](operations.md#retention-and-maintenance).

## Namespaces and grants

Package models and tracking tables follow `generate_schema_name` and `generate_alias_name`. With dbt's standard schema macro, `dbt_dqm_schema: governance` resolves to the target schema plus `_governance`. Custom naming rules can change that result. Pass the same naming vars and target flags to every command using the installation. Changing names creates a different address for the tracking objects. Read [upgrade guidance](operations.md#upgrading) before changing a populated installation.

Grant public-view access through dbt model configuration and configure durable-table privileges with `dbt_dqm_table_grants` when needed. See the [warehouse interface grants](warehouse-interfaces.md#grants) for adapter-specific privilege names and reviewer access.

## Deprecated and obsolete settings

`capture_mode`, `dbt_dqm_capture_mode`, `context_columns`, and `dbt_dqm_reconcile_lookback_days` warn and are ignored. Remove obsolete `poc_responsible`, `owner_column`, `dqm_granularity`, and `dbt_dqm_test_tag`. Use the metadata object, `granularity`, and the fixed `dqm_owner` output column.
