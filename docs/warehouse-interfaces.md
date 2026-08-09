# Warehouse interfaces

## Hook-owned event tables

`dqm_test_executions` stores one idempotent record per invocation and tracked test. It captures dbt
status, failure count, ordered tag JSON, test metadata, timing, message, failure relation, the
outcome of row collection, and a `granularity_signature` (the test's configured
`meta.dbt_dqm.granularity` columns, case-folded and order-preserved) recorded for every conclusive
execution regardless of whether it failed. Reconciliation only treats conclusive, successfully
collected executions as lifecycle evidence, and uses the signature to tell a genuine pass apart from
a grain/identity-scheme change.

`dqm_issue_observations` stores immutable, invocation-level failed identities. Each record contains
the case-insensitive hash, original output display values, collapsed source-row count, initial
annotations, and test tags. `record_values_json` stores one portable JSON object as text, mapping
every stored-failure output column directly to its display value. JSON null and empty string remain
distinct, and names cannot lose alignment with values. `meta.dbt_dqm.granularity` determines the
case-insensitive identity hash independently of the displayed record. The on-run-end hook commits
each test's readable failures (merge plus status update) as soon as that test is processed, rather
than deferring every tracked test's write to one shared batch — a mid-invocation collection error
only leaves the test being processed, and any after it, unresolved at `pending` for a future
invocation to pick up.

## Model-owned lifecycle tables and views

`dqm_issue_occurrences` is the alias of `dqm_reconcile`. It is the durable lifecycle table and owns
the five editable annotations, plus the package-managed `granularity_signature` used to detect
identity-scheme changes. A conclusive pass archives an Active occurrence with `close_reason =
'Passed'`; recurrence creates a new numbered occurrence with clean annotations. Two other archival
paths exist: `close_reason = 'IDENTITY_CHANGED'` when the test's granularity changed since the
occurrence was last touched (the old identity's absence isn't a verified pass), and `close_reason =
'TEST_REMOVED'` when the test itself is no longer present in the project manifest at all.

`dqm_current_issues` exposes Active occurrences for routine review. `dqm_all_issues` exposes the full
Active and Archived history. `dqm_annotation_changes` is the field-level audit ledger populated by
idempotent local-application batches.

Identity and lifecycle columns are package-managed. Only `test_status`, `call_to_action`,
`ticket_url`, `notes`, and `poc_responsible` are editable.
