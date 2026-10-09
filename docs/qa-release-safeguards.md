# Permissions and release safeguards verification

This branch is based on PR #9 at `0243792`. It remains unmerged, untagged and unpublished.
The existing QA and demo datasets have not been reset. The restricted gate upgraded QA to
migration 0004 and left uniquely named synthetic fixtures, annotations and missed reports;
existing history was preserved. The demo dataset was untouched.

## Delivered behavior

- A separate, serial permissions gate uses explicit credentials for the provisioned runner and
  reviewer. It checks `SESSION_USER()` before doing work, leaves synthetic history in QA, and
  tests both successful reviewer operations and denied writes. It never uses the destructive
  acceptance fixture. See [the permission matrix and gate](warehouse-interfaces.md#restricted-bigquery-qa-gate).
- Warehouse downloads and SQLite installation enforce 50,000 issues and 128 MiB of serialized
  data. Limits may only be lowered. Pending issues outside the archive window and pending text
  count toward the final budget. Issue and Health snapshots commit together; rejected refreshes
  leave the cache, Health, timestamp and pending edits intact.
- Existing oversized caches are checked before JSON decoding or DataFrame construction. Their
  pending edits remain reachable in pages of 100 issues. Very large pending values use bounded
  previews instead of being loaded for display.
- Migration `0004_app_staging_safety` adds isolated upload attempts and a warehouse-generated
  timestamp. The logical batch identity stays deterministic. Completed-batch checks, annotation
  updates, audit writes, version increments and successful-attempt deletion share one transaction.
  Failed attempts remain isolated until cleanup; an expired attempt must be uploaded again.
- Staging cleanup runs even without raw/event retention settings. Mixed staging client versions
  are unsupported during rollout: stop reviewer apps, migrate, then restart with the new client.

## Checks on the safeguarded revision

| Check | Result |
| --- | --- |
| Ruff and Python unit/UI tests | Pass; 85 tests |
| Postgres 14.20 acceptance | 17 passed, 387.54 s |
| Postgres 16.11 acceptance | 17 passed, 386.23 s |
| BigQuery staging/migration/concurrent retry cases | 3 passed (812.82 s); 2 stronger populated/partial-audit reruns passed (547.93 s) |
| Existing BigQuery lifecycle/concurrency suite | 14 passed, 4061.37 s (67 min 41 s) |
| Restricted runner/reviewer gate | Pass; 269.60 s, 2026-10-09, actual provisioned runner/reviewer |
| Offline BigQuery compilation | Pass |
| Wheel/source distribution build and wheel CLI check | Pass; artifacts not published |
| GitHub CI for this branch | Not run; branch has not been pushed |

The credentialed mechanics tests use the development service account and disposable datasets
in project `dbt-dqm`, US. They cannot substitute for the restricted-account gate. Postgres tests
use isolated password-protected UTF-8 clusters and disposable schemas.

These runs cover 17 distinct BigQuery cases, with the two stronger audit cases rerun after their
assertions were expanded. The application and package code under test is the final safeguarded
production code (`625dbc6`). The later one-line upload fix at `63f5400` explicitly uses
`CREATE_NEVER`; it passed the restricted gate, Ruff and all 85 unit/UI tests (8.90 s).
The full disposable warehouse suites were not repeated after that upload configuration change.
The permissions gate upgrades QA before ordinary model execution, then uses the reviewer's full
sync entry point, including explicitly authenticated dbt debug/parse subprocesses. The separate
live gate now passes with separately supplied local service-account files. `SESSION_USER()`
verified both identities; the development identity was never substituted. It proved runner
seed/model execution, capture, reconciliation and migration, reviewer full sync and Health reads,
audited notes/verdict updates, idempotent retries and missed reports, and denied reviewer writes
to control/executions/receipts and denied table creation. Its first run exposed a seed/failure-table
name collision; the next exposed the default upload create disposition requesting dataset-create
rights. Both were corrected without broadening IAM, and the complete rerun passed.

## Reviewable commits

| Commit | Scope |
| --- | --- |
| `83caf5d` | Separate restricted-account BigQuery gate and explicit client credentials |
| `d60a4b9` | Streaming issue limits, atomic issue/Health cache installation, pending panel |
| `2fecee2` | Migration 0004, isolated uploads, guarded audit/apply and staging expiry |
| `625dbc6` | Pending-text budget and supported-boundary memory benchmark |
| `52d474f` | Full byte ceiling, retained pending budgets and populated/partial audit tests |
| `3fa04b3` | Verification record, rollout guidance and capture-provenance clarification |
| `9f570de` | Full reviewer sync credentials and correct QA upgrade order |
| `c399236` | Final prior warehouse verification record |
| `63f5400` | Restricted gate fixture naming and existing-table-only reviewer uploads |

The final documentation commit records the completed checks above. Earlier commit messages
describe the checks that were still pending when each chunk was saved; this record supersedes
those pending validation notes. No branch push, merge, tag or package publication has occurred.

### Browser evidence

Real browser checks against an isolated Postgres demo verified:

1. A seven-issue warehouse sync refused by a one-issue limit, preserving the two-issue old cache
   and pending note. The fallback panel displayed the retained edit.
2. Applying that edit while the occurrence table was locked timed out and kept the edit.
3. Retrying after releasing the lock committed the edit once. The subsequent oversized refresh
   was refused and the app explicitly reported the committed Apply. Cache and sync time stayed
   unchanged; the submitted pending edit was cleared.
4. Restarting with the normal cache limit synchronized all seven issues and showed the saved
   note. The temporary browser and server were closed afterward.

### Memory boundary

The real Postgres sync probe at **50,000 issues**, with average captured payloads of 408 bytes,
measured **420.8 MiB peak process RSS**, **2.17 s warehouse reads**, and **5.90 s total** for
read, atomic cache replacement and display preparation. Baseline was 148.8 MiB. A running
Streamlit server adds its own baseline; serialized-size budgets are not a process-memory guarantee.
The reproducible harness and raw measurement are linked from [scale.md](scale.md).

## Remaining release gates and operating limits

- Run GitHub CI on the review branch before merge or release approval. Earlier PR #9 results
  are not CI results for these changes.
- Capture invocations sharing stored-failure tables remain serialized: equal counts do not
  prove provenance. BigQuery reconcile latency/cost and warehouse-side app pagination remain
  separate work.
- Event payload mode remains `full` by default. Configure event retention when values must not
  outlive raw logs. This work does not change event payload defaults or physically cut a network
  connection during COMMIT.
- Existing BigQuery tables can receive clustering metadata without a table swap. More clustering
  experiments remain deferred; see the corrected discussion in [scale.md](scale.md).
