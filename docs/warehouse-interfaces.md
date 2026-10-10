# Warehouse interfaces for 0.2

0.2 requires a fresh `dbt_dqm_schema`. Recognizable 0.1 tables are rejected before an install marker is written. The package does not delete, baseline or migrate existing history. Keep that schema for historical queries and point the new package at another schema. Later upgrades use ordered, verified, idempotent migrations; columns are added and backfilled, never removed automatically. Identity remains `dqm-id-v1` and changes only through an explicit identity scheme version.

## Relations and ownership

| Relation | Ownership and contract |
| --- | --- |
| `dqm_reconcile` | Stable dbt view and runnable reconciliation entry point. |
| `dqm_issue_occurrences` | Hook-owned durable lifecycle table. Only documented annotations are editable. |
| `dqm_all_issues`, `dqm_current_issues` | Public additive 0.x interfaces, reading the durable table directly. A dependency comment orders them after reconciliation; reconciliation alone preserves these views. |
| `dqm_issue_timeline` | Occurrence intervals and latest payload; not a history of every payload version. |
| `dqm_issue_events` | Hook-owned immutable event ledger, written atomically with lifecycle changes. |
| `dqm_annotation_changes` | dbt-owned append-only audit ledger, with a unique batch/occurrence/field key on Postgres. |
| `dqm_missed_issues` | dbt-owned application-written log of known missed issues. |
| `dqm_test_health`, `dqm_area_health` | Warehouse-computed views with explicit populations and rate denominators. |
| `dqm_test_executions`, `dqm_issue_observations` | Hook-owned raw execution and failed-identity evidence. |
| Install, control, migration, run, input, receipt and state tables | Internal coordination interfaces; do not edit directly. |

`dqm_relation(name)` resolves durable tables using the reconcile model's database and schema. The Python application uses the same rule. Package models use static configuration; runtime stages are never model aliases and cannot become stale through partial parsing.

## Evidence and lifecycle

Capture records each tagged, stored-failure test execution. Tracked tests require `fail_calc=count(*)` after whitespace/case normalization, no configured `limit`, and effective table failure storage. Sampling inside test SQL violates this complete-evidence contract. Configuration, collection and inconclusive outcomes never archive issues. A passing test's grain and explicitly configured owner column are checked from column metadata without scanning rows.

Reconciliation freezes all unreceipted conclusive execution IDs. It replays them in `(captured_at, invocation_id)` order. Passing evidence closes an Active issue as `Passed`; identity scheme changes close it as `IDENTITY_CHANGED`; a removed tracked test closes it as `TEST_REMOVED`. New occurrences start with clean annotations. Public history context matches the previous occurrence within the same identity scheme. Recurrence requires its previous closure to be `Passed`; potential regression additionally requires the previous closure workflow snapshot to be `RESOLVED`.

Postgres 14/16 uses a transaction-scoped advisory lock and an EXCLUSIVE occurrence lock, a temporary stage on the same connection, and `ON CONFLICT` lifecycle updates inside dbt's transaction. The temporary stage disappears at commit. On Postgres, `dqm_reconcile` uses the package's `dqm_entry_view` materialization: it takes the lock in the first transactional pre-hook, then replaces the view in place with `CREATE OR REPLACE VIEW`, with no `__dbt_tmp`/`__dbt_backup` relations and no DDL before the lock. dbt's default view materialization drops cached temporary relations before the transactional pre-hooks, so overlapping runs could act on each other's relations (#17). `CREATE OR REPLACE VIEW` requires existing columns to keep their names, order and types; new columns can only be appended, which the additive-only occurrence migrations guarantee. An unexpected table named `dqm_reconcile` is refused rather than dropped. BigQuery keeps its adapter's view materialization, which replaces views atomically. BigQuery uses an invocation-specific stage and one transaction for lifecycle changes, events, receipts, state, generation and completion. Generation, setup readiness and run status guard against stale or abandoned applies; concurrent transactions update the common control row. A known SQL failure abandons the run with a generation increment, allowing a fresh invocation to retry. A lost runner is recovered through timeout abandonment. BigQuery lifecycle and concurrency parity requires credentialed acceptance runs; offline compilation alone does not establish it.

Matched updates preserve all reviewer annotations and annotation versions. `workflow_status_at_close` snapshots the current target workflow atomically on Active→Archived. It does not change afterward. Every rerun freezes again and rebuilds against current state. A successful commit with a lost client response leaves receipted inputs; the next invocation makes no duplicate lifecycle changes.

## Annotations and metadata

The six audited editable fields are `workflow_status`, `review_verdict`, `call_to_action`, `ticket_url`, `notes`, and `poc_responsible`. The app uses compare-and-set against `annotation_version` and writes server-derived old values to the audit ledger in the same transaction. `test_status` remains the deprecated synchronized workflow alias through 0.x.

Verdicts are `UNREVIEWED`, `TRUE_POSITIVE`, or `FALSE_POSITIVE`, independently of workflow. New occurrences start unreviewed. The legacy `FALSE_POSITIVE` workflow option remains available during 0.x.

Row ownership uses explicit case-insensitive `meta.dbt_dqm.owner_column`, otherwise a discovered `DQM_OWNER` column. Trimmed blanks fall back to static `poc_responsible`. Multiple distinct nonblank owners for one identity also use that fallback, record an execution warning, and increment `owner_conflict_identity_count`. Ownership initializes only new occurrences; changing test metadata never replaces reviewer assignments. The owner column does not affect identity or expand payload capture.

`test_priority` is optional `critical`, `high`, `medium`, or `low`; `test_criticality` is an optional string. Active metadata refreshes from the latest processed execution, including clearing removed values. Archived metadata is frozen. Tags remain ordered JSON text.

## History events

Events include `APPEARED`, `REAPPEARED`, `VALUES_CHANGED`, `DISAPPEARED`, `CLOSED_STRUCTURAL`, and `EVIDENCE_SKIPPED`. `dbt_dqm_emit_still_failing_events: true` also emits `STILL_FAILING`. Value changes require `allowlist` or `full` capture.

`dbt_dqm_event_payloads` controls what the event ledger keeps; lifecycle processing always uses the full captured payloads. Each event records the mode it was written with in `payload_mode`.

| Mode | `previous_record_values_json`, `record_values_json` | `changed_columns` (`VALUES_CHANGED` only) | `previous_payload_digest`, `payload_digest` |
| --- | --- | --- | --- |
| `full` (default) | before/after captured values | sorted, comma-separated changed keys | SHA-256 of each payload |
| `changed_columns` | null | sorted, comma-separated changed keys | SHA-256 of each payload |
| `none` | null | null | null |

With the default `full` mode, captured values in events are kept until event retention (`dbt_dqm_event_retention_days`, separate from raw-log retention) removes them. Under `allowlist` or `full` capture those values can include personal data: set event retention or choose a smaller mode. Digests are not anonymization; predictable values can be recovered by hashing guesses. A key missing on one side counts as changed, distinct from an explicit JSON null. Changing the mode affects new events only; migration `0002_event_payload_mode` marks events written before it as `full`. IDs use SHA-256 over the canonical length-prefixed, null-safe `dqm-event-v1` encoding of test, occurrence, original invocation and event type. Skipped evidence has a null occurrence ID and includes the test ID, so skipping two tests from one invocation creates two distinct events.

## Recovery and retention

Late unreceipted evidence at or before the processed high-water fails before lifecycle writes. Review the execution, then explicitly acknowledge a gap:

```sh
dbt run-operation dqm_skip_late_evidence --args '{items: [{test_unique_id: test.example.check, invocation_id: original-run-id}], reason: "Reviewed delayed collection; historical replay intentionally skipped"}'
dbt run-operation dqm_abandon_runs --args '{older_than_minutes: 60}'
dbt run-operation dqm_cleanup_stages
dbt run-operation cleanup_dqm_logs --vars '{dbt_dqm_retention_days: 90}'
```

Skip validates conclusive, late, unreceipted inputs and atomically writes receipts, gap events and a generation increment. Abandon fences the run before dropping its BigQuery stage; resumed runners fail apply. Completed BigQuery stages are eligible for cleanup at least 24 hours after completion; expiration is extended at completion. Failed stage cleanup warns and does not roll back lifecycle changes.

Raw-log retention is opt-in, receipt-aware and portable. Observations, executions and receipts are pruned together only after processing and outside unfinished runs. High-water state is never pruned. Unconditional BigQuery partition expiration is removed. `dbt_dqm_reconcile_lookback_days` is deprecated, warns and is ignored. Events persist indefinitely unless `dbt_dqm_event_retention_days` is configured. Health exposes the raw retention boundary separately from zero observed activity.

### Optional maintenance

After `dbt run`, `build` or `test`, the package runs optional maintenance, but **only when the invocation executed dbt-dqm models or tracked tests**; unrelated runs never touch DQM tables. The steps are:
- receipt-aware raw pruning and run-ledger pruning, when `dbt_dqm_retention_days` is set;
- event retention, when `dbt_dqm_event_retention_days` is set;
- on BigQuery, expiry of app uploads older than 24 hours and drops of stale reconcile stages;
- pruning of maintenance-log rows older than 30 days, whenever any other step is scheduled.

On Postgres without retention settings, nothing is scheduled.

**Maintenance never fails a dbt invocation.** Each step runs in its own protected block:
- On BigQuery, a failing step's open transaction is rolled back before anything else. The rollback is skipped when no transaction is open (BigQuery raises an uncatchable error for that), including after a failed commit; a transaction still open when the script ends is discarded by BigQuery.
- On Postgres, each step is a PL/pgSQL subtransaction inside one transaction that holds the advisory lock.
- A failed step is retried the next time maintenance runs.
- Capture, reconciliation, migrations and setup are **not** wrapped; their failures still fail the run.
- `dbt run-operation cleanup_dqm_logs` runs the same protected steps, then raises if any step failed.

Every step records its outcome in `dqm_maintenance_log` (migration `0005_maintenance_log`):
- The row holds `logged_at`, `invocation_id`, `step`, `outcome` (`succeeded`/`failed`), a fixed `description`, and a `diagnostic_id`.
- The `diagnostic_id` is the BigQuery script job ID (look it up in job history) or the Postgres `SQLSTATE` code.
- **Warehouse error text is never stored**, because it can quote captured values.
- Writing the log is itself protected: if the log can't be written, the run still succeeds.

dbt doesn't display rows returned by hook queries, so the log and the `dqm_maintenance_health` view are the way to observe maintenance. The view (and the app's Health tab) reports each scheduled step as:
- `ok`;
- `failing`, when the latest outcome is a failure;
- `unknown`, when no outcome is recorded or none since the latest DQM execution. A missing observation is never reported as healthy.

On BigQuery, a standalone staging delete can still conflict with a reviewer's Apply at the same moment. Such a conflict is now logged and retried, never fatal to dbt, and the app reports the warehouse as busy for its own retry.

`run --empty` and `build --empty` require initialized current tracking tables. They skip capture, reconciliation, migrations, table grants and cleanup, so no DQM tracking data changes. The public views keep their normal definitions and keep returning real data; ordinary dbt relation DDL (recreating those views) still occurs.

## Capture concurrency

Serialize dbt test invocations that share a stored-failure schema. dbt overwrites each test's failure table, which the on-run-end capture reads; overlapping invocations could otherwise capture another invocation's rows. The reconciliation generation protocol protects apply concurrency, while this capture-side boundary still requires runner coordination.

Capture detects some of these races. Each test's stored failures are copied once into a capture stage inside the capture transaction, and the stage's row count must equal the `result.failures` that dbt reported (0 for a passing test). A mismatch records `collection_status = 'collection_error'` with the message "Stored failures changed before capture", writes no observations, and produces no lifecycle evidence; a mismatched pass is never treated as a pass. Matching counts don't prove the rows came from this invocation (two runs can fail the same number of rows differently), so the serialization requirement above still applies.

## Grants

Public-view grants use normal dbt model configuration. On BigQuery, a package post-hook emits native DCL because dbt-bigquery 1.11 does not apply view grants itself. Roles and principals are quoted; empty principal lists emit no grant. `dbt_dqm_table_grants` maps each table to native privileges and lists of principals. Setup only grants requested privileges; it never revokes unrelated grants.

```yaml
vars:
  dbt_dqm_schema: dqm_v02
  dbt_dqm_table_grants:
    dqm_reconciliation_control:
      select: [dqm_reviewer]
    dqm_issue_occurrences:
      select: [dqm_reviewer]
      update: [dqm_reviewer]
    dqm_annotation_changes:
      select: [dqm_reviewer]
      insert: [dqm_reviewer]
    dqm_missed_issues:
      select: [dqm_reviewer]
      insert: [dqm_reviewer]
```

This is the complete reviewer grant set on Postgres, together with `+grants: {select: [dqm_reviewer]}` on the package models (the public views) and `grant usage on schema <dqm schema> to dqm_reviewer`, which dbt doesn't issue. The acceptance suite proves it with separate non-superuser runner and reviewer roles: the reviewer can sync (which reads `dqm_reconciliation_control` to verify the snapshot), read Health, apply annotations and file missed issues, and is denied every other write and raw-log read. The runner needs only `create` on the database (plus `pgcrypto` already installed).

On BigQuery use native IAM privilege maps, e.g. `roles/bigquery.dataViewer: ["user:reviewer@example.com"]`. The least-privilege identities are:

| Identity | Project | DQM dataset | Tables |
| --- | --- | --- | --- |
| Runner (dbt test/build) | `roles/bigquery.jobUser` | `roles/bigquery.dataEditor`; `roles/bigquery.dataOwner` instead if `dbt_dqm_table_grants` or view `+grants` are configured, because issuing GRANT needs `setIamPolicy`. Plus `dataViewer` on the datasets the tests read. | — |
| Reviewer (review app) | `roles/bigquery.jobUser` | `roles/bigquery.dataViewer`: BigQuery views run with the caller's access to their source tables, and the Health views read executions, receipts, events and `dqm_maintenance_log` | `roles/bigquery.dataEditor` on `dqm_issue_occurrences`, `dqm_annotation_changes`, `dqm_missed_issues` and `dqm_app_change_staging` |

Setup creates `dqm_app_change_staging` (migration `0003_app_change_staging`), so reviewers never need table-create rights on the dataset; the app only loads rows into it. The Postgres reviewer grant set above is proven by an acceptance test with separate restricted roles. The BigQuery matrix has not yet been proven with separate restricted service accounts (the BigQuery suite runs as one elevated identity); see the verification record.

### Restricted BigQuery QA gate

The dedicated gate uses the pre-created `dbt-dqm.dbt_dqm_qa` dataset and exactly
`dbt-dqm-runner@dbt-dqm.iam.gserviceaccount.com` and
`dbt-dqm-reviewer@dbt-dqm.iam.gserviceaccount.com`. Run it alone, with exclusive use of QA:

```bash
DBT_DQM_BIGQUERY_RUNNER_CREDENTIALS=/absolute/path/runner-adc.json \
DBT_DQM_BIGQUERY_REVIEWER_CREDENTIALS=/absolute/path/reviewer-adc.json \
uv run pytest -q integration_tests/acceptance/test_bigquery_permissions.py
```

Credentials are loaded independently per client and passed explicitly to runner subprocesses;
the gate verifies both `SESSION_USER()` identities. It creates uniquely named synthetic fixtures,
leaves history and reports in QA, and never drops or resets that dataset. Do not use the existing
destructive acceptance fixture on QA. Cloud Shell credential files do not exist automatically on
a local workstation. Do not commit credentials.

The runner uses dataset `dataEditor` and no package/view grant configuration. An administrator
applies the four reviewer table grants after initialization. Reviewer dataset `dataViewer` also
permits raw evidence reads; occurrence-table `dataEditor` permits broader writes than annotation
fields. Editable-field restrictions are an application contract, not column-level IAM enforcement.

### Annotation staging lifetime and upgrade

Migration `0004_app_staging_safety` adds `upload_id` and `staged_at` to BigQuery app staging.
JSON uploads omit `staged_at`, which receives a warehouse `CURRENT_TIMESTAMP()` default. Legacy
rows are timestamped once during migration and are never consumed by new app clients. Stop all
reviewer apps, run the package migrations, then restart with the new client; mixed old/new staging
clients are unsupported.

The logical `batch_id` remains deterministic; each upload attempt has its own random ID. Apply
checks the audit ledger inside the same transaction as edits and version increments. A completed
batch is a no-op, and partial audit history is rejected. Successful attempts delete only their
own rows. Failed/uncertain attempts remain isolated and expire after 24 hours; a retry uploads
freshly from local pending edits. Package cleanup deletes expired staging even without raw/event
retention settings. Cleanup requires a package run or `cleanup_dqm_logs`; expiration is not an
autonomous BigQuery timer. No additional reviewer privileges are required.

## Health populations

Defaults are a 30-day reporting window and 14-day stale threshold, configured through positive integer vars `dbt_dqm_metrics_window_days` and `dbt_dqm_stale_days`.

| Metric | Population / denominator |
| --- | --- |
| Reviewed precision | TP / (TP + FP), occurrences first seen in the window. |
| FP share | FP / (TP + FP), same population. |
| Review coverage | Assessed / all occurrences first seen in the window. |
| Time to pass | Passed closures in the window; structural closures excluded. |
| Backlog age, stale NEW, accepted risk | Current Active occurrences. |
| Collection coverage | Conclusive collected executions / recorded executions in the window. |
| Processing coverage | Applied executions / conclusive executions in the window. Skips remain evidence gaps. |
| Processing lag | All retained unreceipted conclusive executions, including those older than the reporting window. |
| Known missed issues | Reports discovered in the window; observed counts, with no recall or false-negative rate. |

All rate denominators are published; zero denominators produce null. Collection coverage does not claim that scheduled tests ran: its denominator is recorded executions. Raw retention limits are explicit. Area health includes root-project models and sources; a test belongs to every direct dependency, so area totals overlap. Unmapped missed reports have separate area rows. No observed failures is informational, not evidence that a test is ineffective. Health snapshots cache these rows and their reporting window/sync time locally; unsaved issue edits remain accessible.

## SQL path for known misses

Use a stable caller-generated ID on retry. Postgres example:

```sql
insert into your_dqm_schema.dqm_missed_issues
  (missed_issue_id,discovered_at,reported_at,reporter,area_label,description,root_cause)
values
  ('stable-uuid',current_timestamp,current_timestamp,current_user,'Unmapped area','Confirmed issue missed by the tests','no_test')
on conflict (missed_issue_id) do nothing;
```

BigQuery callers use a parameterized `MERGE … WHEN NOT MATCHED THEN INSERT` keyed by `missed_issue_id`. Root causes are `no_test`, `test_logic_gap`, `threshold_too_loose`, `test_not_run`, or `other`.
