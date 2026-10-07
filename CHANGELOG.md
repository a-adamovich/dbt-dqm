# Changelog

## 0.2.0 (unreleased)

- Requires a fresh DQM schema; 0.1 history is preserved in its original schema and is not migrated. Later upgrades use verified additive migrations.
- Replaces checkpoint replay and whole-row merges with frozen inputs, transactional lifecycle-only apply, receipts and generation fencing. Reviewer annotations and versions are preserved; closure workflow is captured atomically.
- Adds row owners, conflict warnings, priority/criticality, visible tags, independent review verdicts, recurrence context, timeline and durable payload-change events.
- Adds known missed-issue reporting and warehouse health views with explicit populations, denominators and local offline snapshots.
- Retires reconciliation lookback and unconditional BigQuery raw-log expiration. Receipt-aware retention preserves unprocessed evidence; events have separate opt-in retention.
- Emits native BigQuery table/view grants without revoking unrelated access, and normalizes timestamp arithmetic for portable health and recovery filters.
- Schema-only (`--empty`) runs require initialized current tables, leave tracking data unchanged, and keep the public views serving real data.
- BigQuery adapter parity remains gated on credentialed lifecycle/concurrency tests.
- Capture copies each stored-failure table once and rejects it as `collection_error` when its row count differs from dbt's `result.failures`, including passing tests, so an overwritten table never becomes lifecycle evidence.
- The review app verifies each sync before replacing its cache: setup must be ready, the control generation unchanged across the read, and the issue view's row count must match the tracking table. Inconsistent or unavailable data keeps the cache and pending edits; a verified empty result replaces it.
- Postgres app connections use a lock timeout (`--lock-timeout`, default 5 seconds). Writes blocked by a running reconciliation, and BigQuery transaction conflicts, report the warehouse as busy without changing data. Sync and setup failures show a short message with technical details on request.
- The app reads a Postgres profile's password from either `pass` or `password`, as dbt does.
- `dqm_reconciliation_control` can be listed in `dbt_dqm_table_grants`; reviewers need `select` on it for verified syncs. The documented Postgres reviewer grant set is proven by an acceptance test with separate non-superuser runner and reviewer roles.
- Tracking-table grants are applied only by reconciliation and capture, not also by the parallel app-table models, so BigQuery no longer hits IAM "concurrent policy changes" errors during a package build.
- `dbt_dqm_event_payloads` (`full` default, `changed_columns`, `none`) controls payload retention in events, adding `payload_mode`, `changed_columns` and payload digests. Migration `0002_event_payload_mode` upgrades existing 0.2 schemas in place.
- Migration `0003_app_change_staging` creates the BigQuery app's staging table during setup; the app no longer issues DDL when applying edits, so BigQuery reviewers need no table-create rights on the DQM dataset.
- Reconciliation planning no longer degrades to nested loops on Postgres, and the replay reads only Active rows plus the history of affected identities (see `docs/scale.md`).


## 0.1.1 - Unreleased

### Package

- Reject missing stored-failure relations, invalid/duplicate/empty grains, missing configured
  columns, and inconclusive statuses as lifecycle evidence.
- Introduce byte-exact `dqm-id-v1` identity serialization, cross-adapter golden hashes, and the
  versioned `identity_scheme_signature` contract.
- Replay every unprocessed conclusive execution through a durable per-test checkpoint, preserving
  short fail/pass/recur episodes and making retries idempotent.
- Add identity-only, allowlisted-context, and full-row capture modes; identity-only is the safer
  default. Add portable cleanup, validated cost variables, Postgres indexes, and schema migration
  history.
- Introduce `workflow_status`, annotation compare-and-set versions, server-derived audit values,
  source relation metadata, severity metadata, and documented compatibility aliases.

### App

- Secure workspace directories and SQLite files with owner-only permissions and add `workspace
  info` / `workspace purge` commands.
- Require an explicit reviewer choice for drift conflicts and reject later concurrent annotation
  writes through warehouse compare-and-set.
- Support BigQuery and Postgres app backends, dbt-rendered `env_var()` profiles, and manifest-based
  relation discovery for custom schema naming.

### Project

- Add package-build/metadata/clean-wheel CLI checks, Postgres 14/16 lifecycle matrices, delayed
  reconciliation scenarios, and compile-only BigQuery CI. Pin actions by SHA and declare read-only
  workflow permissions.
- Correct open-source/contribution language and separate the dated historical review from the live
  roadmap.

## 0.1.0 - 2026-08-09

Initial release. Nothing has shipped before this; everything below is new.

### Package

- Add BigQuery-native capture of tagged dbt test executions and complete stored-failure rows.
- Add case-insensitive issue identity, durable occurrences, automatic pass archival, and recurrence.
- Add field-level annotation audit history and active/all issue views.
- Require explicit, exact `meta.dbt_dqm.granularity`; remove legacy key-name inference.
- Add dbt catalog documentation for package models, macros, tests, and demo resources.
- Add a multi-test synthetic BigQuery project spanning one-, two-, and three-column grains and
  two-, three-, and five-column attribute sets.
- Add structured key/attribute JSON and record cards with one bordered `Column | Value` row per
  field for unambiguous multiline and delimiter-containing values.
- Use portable JSON objects for newly collected display fields, add controlled workflow statuses,
  triage metrics and filters, human-readable timestamps, and priority ordering for daily review.
- Replace array-shaped row payloads with portable JSON objects throughout capture and review.
- Remove the obsolete `key_col_names`, `key_col_values`, `attr_col_names`, and `attr_col_values`
  fields. Schema initialization drops them from existing package-owned tables.
- Replace split key/attribute JSON payloads with one `record_values_json` object. Schema migration
  backfills this field before removing `key_values_json` and `attribute_values_json`.
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
- Partition `dqm_test_executions` (by `date(captured_at)`) and `dqm_issue_observations` (by
  `date(observed_at)`), and cluster all three package tables (`test_unique_id` /
  `test_unique_id, unique_id` / `record_status, test_unique_id`) on BigQuery. Add opt-in,
  unset-by-default cost levers: `dbt_dqm_retention_days` (BigQuery partition expiration on the two
  log tables) and `dbt_dqm_reconcile_lookback_days` (lets reconciliation prune via partition
  filtering instead of a full scan). Both preserve current behavior exactly until explicitly
  configured.
- Add Postgres support alongside BigQuery. Replace BigQuery-only SQL (hand-rolled relation naming,
  `int64`/`float64`/`timestamp` literals, `SELECT * EXCEPT(...)`, `TO_JSON_STRING(STRUCT())`,
  `MERGE ... INSERT ROW`) with dbt-core's own portable macros (`dbt.type_string()`,
  `dbt.current_timestamp()`, `dbt.dateadd()`, `api.Relation.create()`) plus a new
  `macros/adapters.sql` `adapter.dispatch()` layer for the handful of things with no portable
  equivalent (SHA-256 hashing, JSON object construction, insert-if-not-matched,
  partitioning/clustering DDL). `dqm_reconcile` and `dqm_annotation_changes` pick their incremental
  strategy per adapter (`merge` on BigQuery, `delete+insert` elsewhere). Add
  `integration_tests/demo_postgres`, mirroring the BigQuery demo; both were run end-to-end
  (including both fault-injection tests above) against live BigQuery and a local Postgres 14
  instance.

### App

- Add a local Streamlit reviewer with SQLite diff staging and background apply/sync.
- Paginate the issue list (25/50/100/200 per page) instead of rendering every filtered issue in
  one script run.
- Debounce full-text search behind an explicit submit (Enter or an Apply button) instead of
  re-filtering and re-stringifying every column of every row on each keystroke. Every other filter
  stays live.
- Share one `ProcessPoolExecutor` across all review sessions via `st.cache_resource` instead of
  creating one per session that was never torn down.
- Fix a read-modify-write race in `store.py`'s `set_change` version counter (two sessions editing
  the same field concurrently could lose an update) using an explicit `BEGIN IMMEDIATE` transaction.
- Add a warning when a pending local edit's recorded base value no longer matches the freshly
  synced warehouse snapshot, instead of silently overwriting the newer remote value on apply
  (`Workspace.drifted_patches`).
- Remains BigQuery-only; adding another warehouse to the app was out of scope for this release.

### Project

- Relicense the entire project (dbt package and local app alike) under Apache-2.0. No code-level
  open/paid split; a future hosted offering, if built, will live and be sold separately.
- Add CI (ruff, pytest, and a full dbt lifecycle run against Postgres in a service container).
- Add `SECURITY.md` and `CODE_OF_CONDUCT.md`; add PyPI classifiers, keywords, and project URLs.
