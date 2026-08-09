# Changelog

## Unreleased

- Add Postgres support to the dbt package alongside BigQuery. Replace BigQuery-only SQL
  (hand-rolled relation naming, `int64`/`float64`/`timestamp` literals, `SELECT * EXCEPT(...)`,
  `TO_JSON_STRING(STRUCT())`, `MERGE ... INSERT ROW`) with dbt-core's own portable macros
  (`dbt.type_string()`, `dbt.current_timestamp()`, `dbt.dateadd()`, `api.Relation.create()`) plus a
  new `macros/adapters.sql` `adapter.dispatch()` layer for the handful of things with no portable
  equivalent (SHA-256 hashing, JSON object construction, insert-if-not-matched,
  partitioning/clustering DDL). `dqm_reconcile` and `dqm_annotation_changes` pick their incremental
  strategy per adapter (`merge` on BigQuery, `delete+insert` elsewhere). Add
  `integration_tests/demo_postgres`, mirroring the BigQuery demo; both were run end-to-end
  (including both Phase 1 fault-injection tests) against live BigQuery and a local Postgres 14
  instance. The local review app remains BigQuery-only; adding a warehouse to it was out of scope.
- Partition `dqm_test_executions` (by `date(captured_at)`) and `dqm_issue_observations` (by
  `date(observed_at)`), and cluster all three package tables (`test_unique_id` /
  `test_unique_id, unique_id` / `record_status, test_unique_id`). Add opt-in, unset-by-default cost
  levers: `dbt_dqm_retention_days` (BigQuery partition expiration on the two log tables) and
  `dbt_dqm_reconcile_lookback_days` (lets reconciliation prune via partition filtering instead of a
  full scan). Both preserve current behavior exactly until explicitly configured.
- Paginate the Streamlit issue list (25/50/100/200 per page) instead of rendering every filtered
  issue in one script run.
- Debounce full-text search behind an explicit submit (Enter or an Apply button) instead of
  re-filtering and re-stringifying every column of every row on each keystroke. Every other filter
  stays live.
- Share one `ProcessPoolExecutor` across all review sessions via `st.cache_resource` instead of
  creating one per session that was never torn down.
- Fix a read-modify-write race in `store.py`'s `set_change` version counter (two sessions editing
  the same field concurrently could lose an update) using an explicit `BEGIN IMMEDIATE` transaction.
- Add a warning when a pending local edit's recorded base value no longer matches the freshly synced
  warehouse snapshot, instead of silently overwriting the newer remote value on apply
  (`Workspace.drifted_patches`).
- Relicense the entire project (dbt package and local app alike) under Apache-2.0, replacing
  FSL-1.1-MIT. No code-level open/paid split; a future hosted offering, if built, will live and be
  sold separately.
- Fix false archival: a test's `meta.dbt_dqm.granularity` change no longer causes its previously
  Active occurrences to be silently archived as a genuine `Passed`. Reconciliation now records a
  `granularity_signature` per execution and per occurrence and archives with `close_reason =
  'IDENTITY_CHANGED'` when the signature differs, instead of `Passed`.
- Fix orphaned occurrences: renaming or removing a tracked test no longer leaves its Active
  occurrences stuck forever. `dqm_reconcile` now sweeps occurrences whose test is absent from the
  current manifest entirely and archives them with `close_reason = 'TEST_REMOVED'`.
- Harden the on-run-end capture hook against partial failures: each test with failing rows commits
  its own observation merge and status update immediately, instead of every tracked test's write
  being deferred to one shared batch at the end of the invocation. A mid-run error now only leaves
  the test being processed (and any after it) at `pending`, rather than losing already-collected
  evidence for tests processed earlier in the same run.
- Normalize identity-hash key values with `TRIM()` before hashing, so incidental leading/trailing
  whitespace no longer produces a different `unique_id` for what a reviewer would consider the same
  key.
- Add a package-level test asserting at most one Active occurrence exists per test-and-identity
  pair.
- Add a generic `ensure_column` migration helper macro, replacing one-off hardcoded migration
  branches for future additive schema changes.
- Turn the BigQuery demo's manual scenario walkthrough into scripted assertions
  (`assert_scenario_outcomes`) that fail the build on a reconciliation regression.

- Replace array-shaped row payloads with portable JSON objects throughout capture and review.
  `unique_id` now hashes the canonical JSON object directly; this is a pre-release breaking change.
- Remove the obsolete `key_col_names`, `key_col_values`, `attr_col_names`, and
  `attr_col_values` fields. Schema initialization drops them from existing package-owned tables.
- Replace split key/attribute JSON payloads with one `record_values_json` object. Schema migration
  backfills this field before removing `key_values_json` and `attribute_values_json`.

## 0.1.0 - Unreleased

- Add BigQuery-native capture of tagged dbt test executions and complete stored-failure rows.
- Add case-insensitive issue identity, durable occurrences, automatic pass archival, and recurrence.
- Add field-level annotation audit history and active/all issue views.
- Add a local Streamlit reviewer with SQLite diff staging and background apply/sync.
- Require explicit, exact `meta.dbt_dqm.granularity`; remove legacy key-name inference.
- Add dbt catalog documentation for package models, macros, tests, and demo resources.
- Add a multi-test synthetic BigQuery project spanning one-, two-, and three-column grains and
  two-, three-, and five-column attribute sets.
- Add structured key/attribute JSON and record cards with one bordered `Column | Value` row per
  field for unambiguous multiline and delimiter-containing values.
- Use portable JSON objects for newly collected display fields, add controlled workflow statuses,
  triage metrics and filters, human-readable timestamps, and priority ordering for daily review.
