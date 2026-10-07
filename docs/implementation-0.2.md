# 0.2 implementation and verification

The four workstreams are implemented in logical commit chunks. Pre-existing working-tree changes are preserved in three separate commits before the 0.2 implementation. Main implementation boundaries:

1. Runtime SQL-returning hooks, stable reconciliation view, frozen inputs/receipts, generation fencing, fresh-install marker, ordered migrations, recovery and receipt-aware retention.
2. Per-row ownership with conflict fallback, clearing/frozen metadata, tags and priority display, and independent audited review verdicts.
3. Recurrence context, timeline, durable before/after payload events, and idempotent missed-issue submissions.
4. Warehouse health populations/denominators and offline app snapshots.

Run local Python checks with `uv run ruff check .` and `uv run pytest -q`. Warehouse acceptance is opt-in and creates/removes isolated synthetic schemas:

```sh
DBT_DQM_TEST_DSN='host=localhost port=5432 dbname=dbt_dqm_ci user=postgres password=dbtdqm' \
  uv run pytest -q integration_tests/acceptance/test_postgres.py
```

CI runs that suite against PostgreSQL 14 and 16. It covers rollback after apply, batched fail/pass/fail, serializing reconciliations, reviewer edits, late skips, retention, future additive migration, schema-only execution, public-view survival, owner conflicts and metadata clearing, structural closures, payload history, verdict audit, and retrying a missed submission.

`uv run python scripts/compile_bigquery.py --profiles-dir …` performs credential-free compilation. dbt-bigquery 1.11 opens an idle connection while closing it; this script prevents that path and rejects all warehouse access. It is restricted to the offline compile process and does not modify runtime adapters. Credentialed BigQuery lifecycle and concurrency acceptance remains a release gate; compilation does not establish parity.

## Local validation result (2026-10-07)

Verified on isolated PostgreSQL 14.20 and 16.11 servers: all nine acceptance tests pass on each. After portable timestamp fixes, targeted lifecycle/health/recovery checks pass on both (three each). All 41 Python/display/store/SQL-render/UI tests pass; Ruff passes. The PostgreSQL package build passes all 23 selected models and data tests. BigQuery parses and compiles offline, including generation fences and isolated runtime stage names.

Real Chrome checks against the disposable PostgreSQL demo confirmed row-owner fallback, metadata badges/filters, audited verdict edits, one missed-report insert, recurrence context/filtering, and cached issues/health after the warehouse became unavailable. The app server and dedicated browser window were closed afterward.

Credentialed BigQuery acceptance passed across the main suite and focused runs (nine distinct scenarios): the main six-case run passed in 1,671.53 seconds; retention, no-write legacy rejection and completed-stage cleanup passed separately. This covers batched replay/rollback/lost-response retries and schema-only execution; reviewer edits, closure snapshots and stale/abandoned fencing; overlapping applies; concurrent initialization and interrupted migration; late skips racing frozen inputs and distinct events for two tests; app verdict auditing, missed-report idempotence and health; receipt-aware retention; no-write legacy/uninitialized rejection; and the completed-stage 24-hour cleanup boundary. Native table/view IAM grants and persisted documentation passed in a dedicated app rerun. These checks use disposable synthetic datasets, not the consumer dataset.

Live runs exposed mandatory BigQuery UPDATE predicates, DATETIME/TIMESTAMP boundary mismatches, and missing adapter-native grant SQL; fixes and regression checks are committed. The first retention assertion was corrected to retain all unprocessed executions, including invalid configurations; the corrected live test passed. PostgreSQL lifecycle/grants also passed after the grant-hook change. A 0.2 app wheel built successfully with verified metadata and app files. After testing, no disposable BigQuery acceptance datasets remained; both temporary PostgreSQL servers, the demo app server and its dedicated browser window were closed.

Version 0.2.0 remains unreleased. Existing 0.1 tracking schemas are rejected without writes; use a fresh DQM schema for this version. Earlier commit messages identify historical snapshots and then-pending checks; this record supersedes those pending validation notes. Guarantees are scoped to the tested adapters, versions and scenarios, and CI retains the credentialed release gate. Serialize captures sharing failure tables as described in the warehouse interface.
