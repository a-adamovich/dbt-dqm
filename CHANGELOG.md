# Changelog

## Unreleased

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
