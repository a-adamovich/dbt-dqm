# Warehouse interfaces

## Stability levels

| Relation | Stability | Contract |
| --- | --- | --- |
| `dqm_current_issues`, `dqm_all_issues` | Public 0.x | Additive columns are allowed; removals/renames require a documented deprecation. |
| `dqm_annotation_changes` | Public 0.x audit | Compound event identity and audit meaning are stable; additive metadata is allowed. |
| `dqm_test_executions`, `dqm_issue_observations` | Advanced 0.x | Useful for operations/debugging; additive schema changes and documented migrations are expected. |
| `dqm_issue_occurrences` | Internal writable | Package/app coordination table; write only documented annotation columns through compare-and-set. |
| `dqm_reconciliation_state`, `dqm_schema_migrations` | Internal | No consumer query contract. |

Within public issue views, lifecycle/identity columns are package-managed and stable through 0.x.
`workflow_status`, `call_to_action`, `ticket_url`, `notes`, and `poc_responsible` are the supported
annotation interface. `test_status` is deprecated and will remain a synchronized alias throughout
the announced 0.x transition; all other additions are backward-compatible.

## Adapter support

BigQuery and Postgres are both supported, verified by an integration demo each
(`integration_tests/demo_bigquery`, `integration_tests/demo_postgres`). The package prefers dbt-core's
own portable macros (`dbt.type_string()`, `dbt.current_timestamp()`, `dbt.dateadd()`, ...) wherever
they exist; the handful of things with no portable equivalent — SHA-256 hashing, JSON object
construction, insert-if-not-matched, and DDL partitioning/clustering — are adapter-dispatched in
`macros/adapters.sql`. Partitioning, clustering, and partition-expiration retention are BigQuery-only
concepts and no-op elsewhere. See the README's "Supported warehouses" section for what's required to
add another adapter.

## Hook-owned event tables

`dqm_test_executions` stores one idempotent record per invocation and tracked test. It captures dbt
status, failure count, ordered tag JSON, test metadata, timing, message, failure relation, the
outcome of row collection, and a `granularity_signature` (the test's configured
`meta.dbt_dqm.granularity` columns, case-folded and order-preserved) plus the broader
`identity_scheme_signature` (algorithm version and ordered grain), recorded for every conclusive
execution regardless of whether it failed. Reconciliation only treats conclusive, successfully
collected executions as lifecycle evidence, and uses the signature to tell a genuine pass apart from
a grain/identity-scheme change. On BigQuery, the table is partitioned by `date(captured_at)` and
clustered by `test_unique_id`.

`dqm_issue_observations` stores immutable, invocation-level failed identities. Each record contains
the case-insensitive hash, capture-mode-selected display values, collapsed source-row count, initial
annotations, and test tags. `record_values_json` stores one portable JSON object as text, mapping
each retained stored-failure output column directly to its display value. JSON null and empty string remain
distinct, and names cannot lose alignment with values. `meta.dbt_dqm.granularity` determines the
case-insensitive identity hash independently of the displayed record. The on-run-end hook commits
each test's readable failures (merge plus status update) as soon as that test is processed, rather
than deferring every tracked test's write to one shared batch — a mid-invocation collection error
only leaves the test being processed, and any after it, unresolved at `pending` for a future
invocation to pick up. On BigQuery, this table is partitioned by `date(observed_at)` and clustered by
`test_unique_id, unique_id`.

Neither log table is pruned automatically; on BigQuery, set the `dbt_dqm_retention_days` project
variable to opt into partition expiration once you've confirmed the window comfortably exceeds how
infrequently your slowest tracked test runs — see `models/dqm_reconcile.sql`'s `latest_executions`
CTE for the matching opt-in `dbt_dqm_reconcile_lookback_days` cost lever (portable — it's a plain
`captured_at` filter, not a BigQuery-specific mechanism), which lets reconciliation prune via
partition filtering on BigQuery instead of a full scan. Both settings reject non-positive or
non-integer values. `dbt run-operation cleanup_dqm_logs --args '{retention_days: 90}'` provides
portable explicit cleanup on both adapters.

`dqm_reconciliation_state` stores the last successfully replayed execution tuple per tracked test.
`dqm_schema_migrations` records completed versioned warehouse migrations.

## Model-owned lifecycle tables and views

`dqm_issue_occurrences` is the alias of `dqm_reconcile`. It is the durable lifecycle table and owns
the five editable annotations, `annotation_version`, and the package-managed identity signatures used to detect
identity-scheme changes. A conclusive pass archives an Active occurrence with `close_reason =
'Passed'`; recurrence creates a new numbered occurrence with clean annotations. Two other archival
paths exist: `close_reason = 'IDENTITY_CHANGED'` when the test's granularity changed since the
occurrence was last touched (the old identity's absence isn't a verified pass), and `close_reason =
'TEST_REMOVED'` when the test itself is no longer present in the project manifest at all.

`dqm_current_issues` exposes Active occurrences for routine review. `dqm_all_issues` exposes the full
Active and Archived history. `dqm_annotation_changes` is the field-level audit ledger populated by
idempotent local-application batches.

Identity and lifecycle columns are package-managed. Only `workflow_status`, `call_to_action`,
`ticket_url`, `notes`, and `poc_responsible` are editable. `test_status` is a synchronized,
deprecated 0.x compatibility alias. Audit rows store server-derived old values plus base and
resulting annotation versions.
