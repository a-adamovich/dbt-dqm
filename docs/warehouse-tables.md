# Warehouse tables

A run leaves a set of tables and views in your warehouse. This page explains what each one holds,
what one row represents, who writes it, and which ones to query. It continues the
`customers_missing_email` example from [Concepts](concepts.md). The exact column contract, grants
and recovery commands are in [Warehouse interfaces](warehouse-interfaces.md).

## Where the tables live

All dbt-dqm relations live in one schema (a dataset on BigQuery), resolved by your own naming
macros:

- By default, they go to the target's schema.
- If you set `dbt_dqm_schema`, dbt's `generate_schema_name` decides the schema. With dbt's standard
  macro, `dbt_dqm_schema: dqm` on target dataset `analytics` gives `analytics_dqm`.
- Table names go through your `generate_alias_name`, including the internal tables, as if they
  were models of the `dbt_dqm` package. A prefixing alias macro therefore prefixes every
  dbt-dqm table.

A schema plus a set of resolved names is one **installation**. A different schema or prefix is a
different installation with its own, separate history. Changing them never moves or copies data.
The `run`, `app` and `workspace` commands must use the same naming variables to reach the same
installation. Source: `dqm_relation` and `relation_contract` in
[`helpers.sql`](../macros/helpers.sql).

The examples below use `my-project.analytics_dqm`.

## Start here: the views

| View | One row is | Use it for |
| --- | --- | --- |
| `dqm_current_issues` | one Active occurrence | The open backlog, with review fields and context from the previous occurrence. |
| `dqm_all_issues` | one occurrence, Active or Archived | Full history. The app syncs from this view. |
| `dqm_issue_timeline` | one occurrence | When each occurrence was valid: `valid_from`, `valid_to`, `close_reason`, `is_current`. |
| `dqm_test_health` | one tracked test | Counts, collection coverage, review precision, staleness and attention flags. |
| `dqm_area_health` | one model or source in your project | The same signals rolled up to the tested model or source. |
| `dqm_maintenance_health` | one maintenance step | Whether each optional maintenance step is `ok`, `failing` or `unknown`. |

`dqm_current_issues` and `dqm_all_issues` add these columns to each occurrence:

- `is_recurrence`: the previous occurrence of this record closed as `Passed`.
- `is_potential_regression`: it was also `RESOLVED` when it closed.
- `previous_occurrence_id`, `previous_archived_at`, `previous_close_reason`,
  `previous_workflow_status_at_close`, `previous_review_verdict`, `previous_dqm_owner`,
  `previous_ticket_url`, `previous_notes`.

The views read `dqm_issue_occurrences` directly, so reviewer edits show up as soon as the app
applies them, with no dbt run in between. The health views have their own populations and
denominators, explained in [Health populations](warehouse-interfaces.md#health-populations).

### Example: the open backlog

```sql
select
  test_name,
  json_value(record_values_json, '$.customer_id') as customer_id,
  occurrence_number,
  first_seen_at,
  last_seen_at,
  workflow_status,
  dqm_owner,
  is_recurrence,
  is_potential_regression
from `my-project.analytics_dqm.dqm_current_issues`
where test_name = 'customers_missing_email'
order by first_seen_at
```

`record_values_json` holds every column the test returned, keyed by the column name, so you can
pick values out with `json_value`. It is text, not a BigQuery `JSON` value.

### Example: one record's history

Each occurrence of `C-1` is one row in the timeline. A recurrence is a new row with the next
`occurrence_number`:

```sql
select occurrence_number, valid_from, valid_to, close_reason, is_current
from `my-project.analytics_dqm.dqm_issue_timeline`
where unique_id in (
  select unique_id
  from `my-project.analytics_dqm.dqm_all_issues`
  where test_name = 'customers_missing_email'
    and json_value(record_values_json, '$.customer_id') = 'C-1'
)
order by occurrence_number
```

For the individual changes inside an occurrence, read its events:

```sql
select event_at, event_type, reason, changed_columns
from `my-project.analytics_dqm.dqm_issue_events`
where occurrence_id = '<occurrence_id>'
order by event_at
```

`dqm_issue_timeline` shows the latest captured values only. To see how values changed, use
`VALUES_CHANGED` events, which keep before and after values if `dbt_dqm_event_payloads` is `full`.

## Tables the app writes

| Table | One row is | Written by |
| --- | --- | --- |
| `dqm_annotation_changes` | one field changed in one apply batch: `batch_id`, `occurrence_id`, `field_name` | The app, in the same transaction that updates the occurrence. It records the old and new value, `changed_by` (the operating-system user running the app) and both annotation versions. |
| `dqm_missed_issues` | one known issue that tests missed, reported from the app | The app. Health counts these per test and area. |
| `dqm_app_change_staging` (BigQuery only) | one staged field change waiting to be applied | The app uploads, the apply transaction deletes. Rows older than 24 hours can't be applied and are expired by maintenance. |

dbt creates the first two as package models and never replaces a populated one, so app-written rows
survive later runs. On BigQuery they use a package materialization for this. On Postgres they are
incremental models with `full_refresh=false`.

## Tables the run writes

These are package-owned. Query them if you need to, but don't edit them.

| Table | One row is | Written by | Notes |
| --- | --- | --- | --- |
| `dqm_issue_occurrences` | one occurrence | Reconciliation (lifecycle fields), the app (the six review fields) | The system of record behind every issue view. |
| `dqm_test_executions` | one tracked test in one dbt invocation | Capture | Status, `collection_status`, `failure_count`, `failure_limit`, `captured_issue_count`, test metadata. On BigQuery, partitioned by day of `captured_at` and clustered by test. |
| `dqm_issue_observations` | one identity failing in one execution | Capture | The captured values and `failure_row_count`. Partitioned by day of `observed_at` on BigQuery. |
| `dqm_issue_events` | one lifecycle event | Reconciliation and recovery | Immutable. Partitioned by day of `event_at` on BigQuery. `invocation_id` is the capture invocation that caused the event. For `TEST_REMOVED` closures it is the reconciliation run. |
| `dqm_reconciliation_runs` | one reconciliation attempt | Reconciliation | `started`, `completed` or `abandoned`, with the generation it froze. |
| `dqm_reconciliation_inputs` | one execution frozen by one run | Reconciliation | |
| `dqm_reconciliation_receipts` | one processed execution | Reconciliation and recovery | `applied`, or `skipped_late` with the reason given to `dqm_skip_late_evidence`. |
| `dqm_reconciliation_state` | one tracked test | Reconciliation | The latest processed execution, used by the late-evidence check. Never pruned. |
| `dqm_install` | the installation (one row) | Setup | Marker `dqm-full-v1` and `installation_id`. A full refresh assigns a new ID. |
| `dqm_reconciliation_control` | the installation's control state (one row) | Setup, reconciliation, recovery | `generation`, `setup_status`, `schema_version`, the migration lease and the raw-pruning boundary. |
| `dqm_schema_migrations` | one applied migration | Setup | See [Legacy code](legacy.md). |
| `dqm_maintenance_log` | one maintenance step outcome | Maintenance | Fixed descriptions and diagnostic IDs only, never warehouse error text. |
| `dqm_reconcile_stage_<run>` (BigQuery only) | one staged change | Reconciliation | One table per run, expiring after 24 hours. |

`dqm_reconcile` is a view with the same rows as `dqm_issue_occurrences`. It exists so reconciliation
has a dbt model to hang its hooks on.

**Why the events and the observations are separate.** Observations are the raw evidence: one row
per failing identity per run, which grows quickly for a test that fails every day. Events record
only changes. Occurrences summarize both. That's why their retention is set separately.

## Retention

Nothing is deleted unless you configure it.

- `dbt_dqm_retention_days` prunes raw evidence: executions, observations and receipts, together and
  only after reconciliation has processed them. Occurrences and the per-test high-water mark are
  never pruned.
- `dbt_dqm_event_retention_days` prunes events. Events are the readable history, so you may want to
  keep them longer than raw evidence.
- `dbt_dqm_event_payloads` decides what an event keeps: `full` (before and after values, the
  default), `changed_columns` (only the names of changed columns, with digests) or `none`.
  Lifecycle processing always uses full values. The setting only affects what events store.

Captured values can contain personal data, and with `full` event payloads they stay in events until
event retention removes them. A run warns when raw retention is unset, and when event retention is unset while event payloads are `full`. Digests aren't
anonymization, because predictable values can be recovered by hashing guesses.

## Grants

Use `dbt_dqm_table_grants` to grant specific tables, and normal dbt `grants` on the views.
[Grants](warehouse-interfaces.md#grants) lists what a runner and a reviewer need, including the
restricted BigQuery setup.
