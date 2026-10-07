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

Postgres 14/16 uses a transaction-scoped advisory lock and an EXCLUSIVE occurrence lock, a temporary stage on the same connection, and `ON CONFLICT` lifecycle updates inside dbt's transaction. The temporary stage disappears at commit. BigQuery uses an invocation-specific stage and one transaction for lifecycle changes, events, receipts, state, generation and completion. Generation, setup readiness and run status guard against stale or abandoned applies; concurrent transactions update the common control row. A known SQL failure abandons the run with a generation increment, allowing a fresh invocation to retry. A lost runner is recovered through timeout abandonment. BigQuery lifecycle and concurrency parity requires credentialed acceptance runs; offline compilation alone does not establish it.

Matched updates preserve all reviewer annotations and annotation versions. `workflow_status_at_close` snapshots the current target workflow atomically on Active→Archived. It does not change afterward. Every rerun freezes again and rebuilds against current state. A successful commit with a lost client response leaves receipted inputs; the next invocation makes no duplicate lifecycle changes.

## Annotations and metadata

The six audited editable fields are `workflow_status`, `review_verdict`, `call_to_action`, `ticket_url`, `notes`, and `poc_responsible`. The app uses compare-and-set against `annotation_version` and writes server-derived old values to the audit ledger in the same transaction. `test_status` remains the deprecated synchronized workflow alias through 0.x.

Verdicts are `UNREVIEWED`, `TRUE_POSITIVE`, or `FALSE_POSITIVE`, independently of workflow. New occurrences start unreviewed. The legacy `FALSE_POSITIVE` workflow option remains available during 0.x.

Row ownership uses explicit case-insensitive `meta.dbt_dqm.owner_column`, otherwise a discovered `DQM_OWNER` column. Trimmed blanks fall back to static `poc_responsible`. Multiple distinct nonblank owners for one identity also use that fallback, record an execution warning, and increment `owner_conflict_identity_count`. Ownership initializes only new occurrences; changing test metadata never replaces reviewer assignments. The owner column does not affect identity or expand payload capture.

`test_priority` is optional `critical`, `high`, `medium`, or `low`; `test_criticality` is an optional string. Active metadata refreshes from the latest processed execution, including clearing removed values. Archived metadata is frozen. Tags remain ordered JSON text.

## History events

Events include `APPEARED`, `REAPPEARED`, `VALUES_CHANGED`, `DISAPPEARED`, `CLOSED_STRUCTURAL`, and `EVIDENCE_SKIPPED`. `dbt_dqm_emit_still_failing_events: true` also emits `STILL_FAILING`. Value changes retain before/after payloads and require `allowlist` or `full` capture. IDs use SHA-256 over the canonical length-prefixed, null-safe `dqm-event-v1` encoding of test, occurrence, original invocation and event type. Skipped evidence has a null occurrence ID and includes the test ID, so skipping two tests from one invocation creates two distinct events.

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

`run --empty` and `build --empty` require initialized current tracking tables. They skip capture, reconciliation, migrations, table grants and cleanup, and project zero rows. Ordinary dbt relation DDL still occurs; no DQM tracking-data changes occur.

## Capture concurrency

Serialize dbt test invocations that share a stored-failure schema. dbt overwrites each test's failure table, which the on-run-end capture reads; overlapping invocations could otherwise capture another invocation's rows. The reconciliation generation protocol protects apply concurrency, while this capture-side boundary still requires runner coordination.

## Grants

Public-view grants use normal dbt model configuration. On BigQuery, a package post-hook emits native DCL because dbt-bigquery 1.11 does not apply view grants itself. Roles and principals are quoted; empty principal lists emit no grant. `dbt_dqm_table_grants` maps each table to native privileges and lists of principals. Setup only grants requested privileges; it never revokes unrelated grants.

```yaml
vars:
  dbt_dqm_schema: dqm_v02
  dbt_dqm_table_grants:
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

On BigQuery use native IAM privilege maps, e.g. `roles/bigquery.dataViewer: ["user:reviewer@example.com"]` and `roles/bigquery.dataEditor` on writable tables. The runner also needs dataset table creation and control/migration permissions. The app needs reads on public issue/health views and direct reads of occurrences/audit, occurrence updates, audit inserts and missed-issue inserts. The BigQuery app's existing annotation staging path additionally needs staging-table create/load/read access and jobs.create; Postgres does not create app staging tables.

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
