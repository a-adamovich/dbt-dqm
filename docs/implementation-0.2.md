# 0.2 implementation and verification

For the subsequent permissions and cache/staging safeguards, see the
[current verification record](qa-release-safeguards.md). That record supersedes older app-memory
and staging limitations below; restricted BigQuery permissions remain a separate live gate.

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

## Initial implementation validation (2026-10-07, superseded below)

Verified on isolated PostgreSQL 14.20 and 16.11 servers: all nine acceptance tests pass on each. After portable timestamp fixes, targeted lifecycle/health/recovery checks pass on both (three each). All 41 Python/display/store/SQL-render/UI tests pass; Ruff passes. The PostgreSQL package build passes all 23 selected models and data tests. BigQuery parses and compiles offline, including generation fences and isolated runtime stage names.

Real Chrome checks against the disposable PostgreSQL demo confirmed row-owner fallback, metadata badges/filters, audited verdict edits, one missed-report insert, recurrence context/filtering, and cached issues/health after the warehouse became unavailable. The app server and dedicated browser window were closed afterward.

Credentialed BigQuery acceptance passed across the main suite and focused runs (nine distinct scenarios): the main six-case run passed in 1,671.53 seconds; retention, no-write legacy rejection and completed-stage cleanup passed separately. This covers batched replay/rollback/lost-response retries and schema-only execution; reviewer edits, closure snapshots and stale/abandoned fencing; overlapping applies; concurrent initialization and interrupted migration; late skips racing frozen inputs and distinct events for two tests; app verdict auditing, missed-report idempotence and health; receipt-aware retention; no-write legacy/uninitialized rejection; and the completed-stage 24-hour cleanup boundary. Native table/view IAM grants and persisted documentation passed in a dedicated app rerun. These checks use disposable synthetic datasets, not the consumer dataset.

Live runs exposed mandatory BigQuery UPDATE predicates, DATETIME/TIMESTAMP boundary mismatches, and missing adapter-native grant SQL; fixes and regression checks are committed. The first retention assertion was corrected to retain all unprocessed executions, including invalid configurations; the corrected live test passed. PostgreSQL lifecycle/grants also passed after the grant-hook change. A 0.2 app wheel built successfully with verified metadata and app files. After testing, no disposable BigQuery acceptance datasets remained; both temporary PostgreSQL servers, the demo app server and its dedicated browser window were closed.

Version 0.2.0 remains unreleased. Existing 0.1 tracking schemas are rejected without writes; use a fresh DQM schema for this version. Earlier commit messages identify historical snapshots and then-pending checks; this record supersedes those pending validation notes. Guarantees are scoped to the tested adapters, versions and scenarios, and CI retains the credentialed release gate. Serialize captures sharing failure tables as described in the warehouse interface.

## Hardening verification (PR #9, 2026-10-07)

This section supersedes the results above. All results are from commit `45f0038`, the last package or app code change. Later commits
change only documentation and the BigQuery test's per-command timeout.

### Environment

- Python 3.11.15
- dbt-core 1.11.11, dbt-postgres 1.11.0, dbt-bigquery 1.11.3
- google-cloud-bigquery 3.43.0, psycopg2-binary 2.9.12, pandas 2.3.3, streamlit 1.61.1
- PostgreSQL 14.20 and 16.11, built from source on Apple silicon. The local clusters use
  scram-sha-256 password authentication and UTF-8, matching CI's `postgres` images.
- BigQuery on-demand in the US multi-region, project `dbt-dqm`, using one service account. Every
  test uses a disposable dataset.

### Results at 45f0038

| Check | Result |
| --- | --- |
| `ruff check .` | pass |
| Unit tests (`tests/`) | 57 passed |
| PostgreSQL 14.20 acceptance | 17/17 passed (404 s) |
| PostgreSQL 16.11 acceptance | 17/17 passed (398 s) |
| BigQuery acceptance, credentialed | 13/14 passed in the full run (3,973 s). `test_bigquery_overlapping_applies` passed its concurrency assertions, but its follow-up clean reconcile hit the test's 300 s per-command subprocess timeout. Job history showed no hung or queued job: every script finished, the longest in 126 s, typically 85–100 s. With the timeout raised to 600 s (test-only change), the test passed on rerun (227 s). |
| GitHub Actions on `45f0038` | all 5 checks passed: [run 37686440376](https://github.com/a-adamovich/dbt-dqm/actions/runs/37686440376) |

**Postgres acceptance:**
- the lifecycle, annotations, history and Health
- batched replay, rollback, and `--empty` (public views keep their data)
- serialized reconciliations with a surviving reviewer edit
- late skip, cleanup and a future migration
- the uninitialized, empty and legacy guards
- owner conflicts, payload events and structural closures
- app verdict audit, missed-issue submission and shared dependencies
- interrupted first setup
- Health counting outstanding evidence
- tampered failure tables, for both a failing and a passing test
- app writes failing fast while a reconciliation holds the table
- a capture that completes after a later run is late evidence
- separate non-superuser runner and reviewer roles, using only the documented grants
- all three event payload modes
- migration `0002` on a populated 0001 schema, with an interruption and a retry

**BigQuery acceptance:**
- batched replay, rollback, a lost commit response and `--empty`
- stale-generation and abandoned-runner fencing with annotations preserved
- overlapping applies
- concurrent installation and an interrupted migration
- a skip invalidating frozen inputs
- the app's verdict audit, missed-issue retries, Health and native grants
- receipt-aware retention
- no-write legacy and uninitialized rejection
- the completed-stage retention boundary
- tampered failure tables
- event changed-columns and digests
- **a stale migration owner resuming after takeover:** it can't backfill, record or release
- **strict `--empty`:** every `dqm_` table's row count and content fingerprint is unchanged
  after both `run --empty` and `build --empty`, including annotations, audit history and missed
  issues
- **`0002` on a populated schema**, with an interruption, a refused live-lease retry, and a
  takeover after the lease expired

**Browser QA** of `dbt-dqm app` against a Postgres demo, before the final readability-only commit:
- a verified sync
- a sync refused after the issue view was redefined to return no rows, with the cache kept
- "busy" on Apply while the occurrence table was locked, with the edit kept pending
- a successful retry after the lock was released
- the stale action bar fixed

### Fixes found during hardening

- **CI failure:** the acceptance fixture dropped the password, because `get_dsn_parameters()`
  omits it.
- **Capture** had no stored-failure consistency check.
- **`--empty`** left the public views returning no rows.
- **The app** could hang behind the reconciliation lock, replaced its cache with unverified
  data, read only `pass:` and not `password:` from Postgres profiles, and left a stale action bar
  on screen.
- **Grants:** reviewers couldn't be granted the control table, and on BigQuery the parallel
  models granted the same tables at once ("concurrent policy changes").
- **BigQuery staging:** the app needed table-create rights on the dataset to stage edits; fixed
  by migration `0003`.
- **Postgres planner:** nested loops made a mass pass unbounded.
- **Wasted reads:** the replay read the whole occurrence history, and on BigQuery every
  observation partition.

The method and numbers are in [scale.md](scale.md).

### Remaining limitations

- **Capture provenance:** the row-count check detects some capture races, but equal counts don't
  prove the rows came from this invocation. Runs that share failure tables must still be
  serialized.
- **BigQuery permissions are unproven:** no test yet uses separate restricted runner and reviewer
  service accounts. The documented matrix is proven on Postgres only.
- **BigQuery concurrency:** overlapping runs are detected and aborted by the generation check and
  transaction conflicts. One reconciliation per dataset remains the supported operating mode.
- **Lost commit response** is simulated by discarding a successful response and retrying. A
  network cut during COMMIT was not physically tested.
- **App memory** is about 6.9 KB per cached issue with ~400-byte payloads. The supported cache is
  about 50,000 issues (Active plus the archive window). Warehouse-side pagination stays deferred,
  and memory is an open concern.
- **BigQuery cost** grows linearly with the occurrence table: about 1.5 GB per reconcile at 1M
  occurrences. Clustering was measured and not added.
- **Event retention:** event values are kept indefinitely unless `dbt_dqm_event_retention_days`
  is set. `full` stays the default payload mode in this release.
- **The Postgres planner setting** costs steady-state time to avoid unbounded mass passes. Choosing
  it per run is a possible refinement.
- **BigQuery wall time** is dominated by per-statement latency: one reconcile is two scripts of
  about 40 statements, taking 85–126 s each in these runs.
- **Not covered:** the VCG migration, warehouse-side pagination, and a 0.1 → 0.2 data migration (0.2
  requires a fresh schema).
