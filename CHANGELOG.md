# Changelog

## 0.2.2 — Unreleased candidate

- Add a dbt 2.0.9 BigQuery path through a shared CLI/artifact interface, retaining dbt Core
  1.11–1.12 on BigQuery and Postgres. Credentialed BigQuery validation is a release gate;
  offline checks alone do not establish full v2 compatibility.
- Select a separately installed engine with `--dbt-executable` or `DBT_DQM_DBT_EXECUTABLE`.
  App extras now install warehouse clients without installing dbt or its Python adapters.
- Select bounded test capture inside the package materialization, preserve fresh invocation
  evidence, and remove app configuration dependencies on dbt's Python internals.
- Preserve the warehouse schema, issue identities, annotations, and workspace identity.


## 0.2.1 - 2026-10-19

- Fix BigQuery initialization in datasets with default `und:ci` collation. Create tracking, app,
  reset and staging tables with empty collation and strip inherited string collation for comparisons.
- Preserve case-sensitive payload changes and owner conflict detection while retaining existing
  identity normalization. Add credentialed lifecycle and explicit recovery regression coverage.
- Refuse existing collated tracking columns before writes and document confirmed scoped recovery.
  Uncollated installations upgrade without a migration or history reset.

## 0.2.0 - 2026-10-15

- First stable release of the 0.2 series, with the behavior validated in 0.2.0a8.
- Package the matching Python app and dbt project as version `0.2.0`; prepare Hub registration
  after the public GitHub release. Install the dbt package from the Git tag until Hub is live.
- Upgrading both components from 0.2.0a7 or 0.2.0a8 requires no warehouse migration or full
  refresh. Keep the [migration guidance](docs/operations.md#upgrading) for older alphas.

## 0.2.0a8 - 2026-10-15

- Extend `run --summary` with OPENED (NEW), RECURRING and ARCHIVED changes applied by the
  reconciliation, grouped by saved owner alongside NEW (status) and ACTIVE workload counts.
  Include archived-only owners and refuse stale-generation reports; record results in metrics.
- Sort quiet-mode test results by status priority and test name, retaining failure details.
- Report collections reaching their native limit in the completion line, name affected tests,
  and record counts and identities in metrics using the existing collection query.

## 0.2.0a7 - 2026-10-15

- Apply consumer alias naming to package state, upload and reconciliation staging tables, and use
  the same resolved contract for app reads/writes, reset, migration checks and maintenance.
  Scope Postgres locks/indexes and local workspaces to the installation.
  Resolve naming offline with private artifacts, preserving consumer manifests during concurrent reads.
- Refuse implicit initialization when corrected naming would abandon legacy shared history.
  Require a confirmed fresh start of the selected namespace; preserve legacy and other-prefix data.
  Install the matching app and dbt package and pass the same `--vars` to run, app and workspace.
- Add optional `run --summary`: current NEW/ACTIVE counts by saved owner, Total first, sorted by
  NEW descending. ACTIVE includes NEW; these are workload counts, not invocation deltas.
- Reorder CSV/Excel columns for review; preserve Excel granularity/record field order and label
  timestamp zones explicitly without altering values or captured JSON.
- Emit one quiet-mode status/row-count line per test; retain full dbt output under `--verbose`.

## 0.2.0a6 - 2026-10-14

- Preview fully resolved tracking relations and confirm destructive full refreshes; add `--yes`
  for unattended runs. Keep validation before reset and record declined runs as cancelled.
- Add bounded cross-page issue selection and atomic staging of bulk owner/status/verdict edits.
  Preserve other drafts, compare-and-set conflicts, audit history and idempotent Apply retries.
- Add background CSV and Excel exports of all filtered saved issues across pages. Preserve JSON
  in CSV; expand top-level captured fields on one Excel worksheet per test. Guard size, query
  budgets and installation/generation consistency; do not return partial or truncated reports.
- Avoid Windows attribute changes on active SQLite journals and tolerate disappearing Unix
  journal side files; keep exports under the private workspace directory.
- Upgrading from a4 or a5 requires no warehouse migration or history reset. Scheduled full-refresh
  invocations must now include `--yes`.

## 0.2.0a5 - 2026-10-14

- Capture routine dbt output; add `--verbose`, phase progress, test/capture summaries and failure metrics.
- Add defer/state flags and a shared artifact-directory override for the runner and review app.
  Reject input-state/output-directory collisions before parsing, and keep DQM writes in the selected target.
- Accept inherited metadata owner lists, preserving all nonblank owners as comma-separated text.
  Invalid values identify their effective metadata key and type; background failure logs always include a reason.
- Avoid redundant collection setup lookups and restrict eager adapter caching to selected resources.
  Add opt-in BigQuery maintenance eligibility shortcuts; preserve retention fences and attributed health.
- Expand the capture benchmark to matched fresh, refresh, unchanged and retention-backlog trials,
  with an optional installed a4 baseline. No warehouse schema migration or history reset is required.

## 0.2.0a4 - 2026-10-13

- Add explicitly enabled `dbt-dqm run`; remove automatic capture/retention end hooks. Package models
  and configured DQM tests remain disabled during ordinary commands. Membership uses `meta.dbt_dqm`.
- Capture every output attribute; native positive test limits default to 100, with deterministic
  samples and incomplete-evidence handling. Limited runs never infer resolution from omitted rows.
- Replace ownership with `dqm_owner`, falling back to `meta.dbt_dqm.owner` then `meta.owner`.
  Obsolete ownership/granularity/tag aliases are unsupported; capture mode settings warn and are ignored.
- Require explicit full refresh for the new schema contract, preserving unrelated warehouse objects
  and table access. Installation identities reject stale reviewer submissions after reset.
- Keep issue cards ordered by priority, creation time and ID during edits and Apply. Hide the old
  `dqm` marker from business-tag displays. Remove unsupported macro argument documentation types.


## 0.2.0a3 - 2026-10-13

- Warehouse adapters are optional: install `dbt-dqm[bigquery]`, `dbt-dqm[postgres]`,
  or `dbt-dqm[bigquery,postgres]`. Plain installs include no warehouse adapters.
- Missing adapters produce an installation hint; local workspace commands work without adapters.
- No warehouse schema migration is introduced.

- CLI options are optional and follow dbt's project, profile and target defaults and environment.
- Resolves the active environment's dbt executable on Windows as well as macOS/Linux; startup logs
  its path and version, and errors preserve full command diagnostics.
- Worker errors are classified before crossing process boundaries, preserving BigQuery errors,
  cache-limit switching and committed-Apply refresh outcomes.
- Checks required app relations and reports an initialization command for missing tables/views.
- Orders all setup-writing models before reconciliation so BigQuery package initialization can use
  normal multithreading.
- Supports consumer model aliases throughout package SQL and app relation lookup, including
  unique indexes when canonical and aliased tables coexist. Existing tables and data are preserved.
- Renders selected profile values once, preserving literal credentials from environment variables.
- Adds Windows/macOS CI with native configuration, spawn workers and four clean adapter variants.

## 0.2.0a2 - 2026-10-12

- Supports Python 3.11–3.14 (`>=3.11,<3.15`), with Python 3.14 recommended.
- Requires mashumaro 3.17 and Streamlit 1.52 or newer for Python 3.14 compatibility.
- Repairs the AI Platform/GenAI dependency branch while retaining other compatible locked versions.
- Tests every supported Python minor in CI, including clean wheel installs, BigQuery compilation,
  and Postgres 14/16 acceptance. Release and scheduled BigQuery verification use Python 3.14.
- No warehouse schema migration is introduced.

## 0.2.0a1 - 2026-10-11

First Python alpha and matching dbt Git package release. Interfaces may change. The Python
app and dbt package are installed separately; 0.1 users need a fresh DQM schema. The previous
0.2.0 heading represented unreleased development, now included here.

- WIF is supported through externally supplied ADC with an `oauth` profile. Offline compatibility
  coverage uses simulated token exchange; live WIF remains unverified. Release BigQuery QA uses
  existing local service-account credentials without creating or downloading keys.
- The GitHub release is a prerelease; approval and provenance verification remain required.
- The alpha tag uses Python version spelling and is not accepted by dbt Hub; install this alpha
  through Git even after repository registration. dbt only accepts SemVer, so `dbt_project.yml`
  spells the version `0.2.0-a1`; the release check requires exactly that pairing.
- Only pre-release tags create a GitHub pre-release; stable tags keep the generated release notes.
- `dbt_dqm_schema` now places the package's tables and views. It was set in the package's own
  `dbt_project.yml`, which dbt renders without the consumer's vars, so the documented setting was
  silently ignored and everything went to the target schema. 0.1 users could therefore not follow
  the fresh-schema instruction. The schema is now set in each package model.

- `--browse auto` is now the default: the app syncs a local snapshot while the review population fits the cache limits (50,000 issues / 128 MiB), as before, and reads issues from the warehouse page by page above them instead of refusing. `--browse snapshot` restores the old behaviour. Evidence: `docs/scale.md` (Warehouse browsing, 1M occurrences on both adapters).
- Warehouse browsing on BigQuery reads pages in chunks of 2,000 issues and pages through each chunk locally. BigQuery bills the referenced columns of every row whether a read returns 25 rows or 2,000, so a page used to cost about 0.96 GB at 1M occurrences; a chunk costs the same and serves 80 pages of 25 (about 12 MB and 0.1 s per page amortized). Postgres still reads one page per statement.
- Migration `0007_browse_keyset_index`: on Postgres, a partial expression index on the warehouse-browsing sort keys (Active issues only), built under the reconcile lock (1.5–5 s at 1M occurrences). At 1M occurrences / 100k Active it takes an unfiltered page from about 2.7 s to 0.02 s. On BigQuery the migration records the version only. Warehouse-browsing costs and latencies at 1M occurrences are in `docs/scale.md`, including BigQuery's about 1 GB per page, which the next change addresses.
- The scale harnesses measure warehouse browsing (`--browse`, `integration_tests/scale/browse_probe.py`), and their synthetic history now varies workflow status, priority, owner and tags.
- Warehouse browsing reads are faster: each page, summary and filter-choice read carries the control generation and setup status in the same statement (one snapshot) instead of separate checks before and after. A Postgres page is one statement (was three), a BigQuery page two jobs (was four) and a summary one (was four); on BigQuery each job adds about a second.
- Warehouse browsing works offline: when the warehouse can't be read, the app shows the recently loaded pages it has cached (up to 2,000 issues) with local filters, labelled "Offline" with the time of the last load. Edits are kept locally, Apply is disabled until a "Retry the warehouse" or Sync succeeds.
- Warehouse browsing in the app, as a preview: `--browse warehouse` reads issues one page at a time from the warehouse (filters, search, counts and the sort run there), and `--browse auto` uses a snapshot while the population fits the cache limits and switches to paging above them. The default is `auto`. Pages are cached locally up to 2,000 issues or 32 MiB, never dropping issues with pending edits, so edits are staged and applied with the same compare-and-set versions as before; a reconciliation between pages shows a "restart from the first page" banner. In warehouse mode, Sync and Apply refresh Health and re-read the page on screen instead of downloading every issue.
- Warehouse-side issue browsing (`docs/design/warehouse-browsing.md`): parameterized filter, search, keyset-page, count and metrics queries for both adapters, verified against the control generation like a sync, with BigQuery pages read in two phases (keys, then one page of rows). A BigQuery search is estimated with a dry run and runs with `maximum_bytes_billed`; the new `--max-search-gib` (default 1) sets the ceiling. The app uses these queries in warehouse browsing mode.
- Requires PyArrow 25.x (`>=25.0.1,<26`). Peak sync memory at the 50,000-issue boundary is equal or slightly lower than with 23.0.1 (`docs/scale.md`).
- Supports pandas 3 (`pandas>=2.2,<4`; the lockfile uses 3.0.6). pandas 3 stores missing strings as NaN, which made never-edited issue cards show "User updated: nan"; card and filter helpers now treat None, NaN, NaT and `pd.NA` alike as missing. The app keeps object-backed strings, because pandas 3's Arrow-backed default added about 70 MiB of peak memory at the 50,000-issue boundary (`docs/scale.md`).
- The app no longer reports an unsupported dbt version or invalid `dbt_project.yml` as a missing profile: the profile hint matched dbt's own "profiles.yml file [OK found and valid]" line. dbt output is matched without its colour codes.
- Supports dbt-core 1.12 (the lockfile now uses dbt-core 1.12.5 and dbt-bigquery 1.12.1; dbt-postgres stays on 1.11, which supports dbt-core 1.12). `require-dbt-version` widens to `>=1.11.0, <1.13.0`. All protocol SQL and compiled package nodes render identically under 1.11.15 and 1.12.5 on both adapters. dbt-core 1.12 brings new transitive dependencies (metricflow, sqlglot, rapidfuzz, opentelemetry-api, python-dotenv, tabulate, dbt-core-experimental-parser).
- The scheduled BigQuery workflow is keyless (Workload Identity Federation, no stored key), runs only from `main` when enabled, checks that its identity can't read the shared QA dataset, runs the parallel acceptance suite, and reports failures in an issue. One-time setup: `docs/scheduled-bigquery.md`.
- BigQuery maintenance uses fewer jobs per invocation: the trigger marker is read by the first step's job, and one protected job logs every outcome after the steps (from 1 + 2n jobs to n + 1). Each step still runs as its own job, separate from the logging.
- The credentialed BigQuery acceptance suite runs in parallel (`pytest -n 6`, about 25 minutes instead of 2 h 19 m). Every disposable dataset is labelled with its run, and leftovers from killed workers or stopped runs are swept automatically (`integration_tests/acceptance/bigquery_datasets.py`).

- Releases check that the tag matches the `pyproject.toml` and `dbt_project.yml` versions before anything is built, and install from the lockfile (`uv sync --locked`).
- Requires `sqlparse>=0.6.0`, `urllib3>=2.8.0` and `oauthlib>=4.0.0` directly, the first versions without the advisories resolved in #24. The lockfile already used them, but a wheel installed without the lockfile could keep older, vulnerable versions that still satisfied the upstream ranges.
- Postgres reconciliation replaces the `dqm_reconcile` view in place after taking its lock (`dqm_entry_view` materialization), so overlapping runs no longer act on each other's temporary relations (#17).
- Optional maintenance (retention, staging expiry, stage drops) runs only after invocations that captured tracked tests or completed a reconciliation. A SQL error inside a step never fails the dbt invocation (connection, cancellation and lock failures still do). Outcomes go to `dqm_maintenance_log` (migration `0005`) with sanitized descriptions and safe diagnostic IDs, and appear as `ok`/`failing`/`unknown` in `dqm_maintenance_health` and the app's Health tab (#13).
- Maintenance outcomes are now trustworthy: on BigQuery each step and its outcome log run as separate jobs; a failed stage drop fails `stage_drops` and keeps its run for a retry; `cleanup_dqm_logs` requires a recorded success for every scheduled step; and migration `0006_maintenance_attribution` records each outcome's trigger, so Health compares outcomes with the invocations that should have produced them (contract in `docs/warehouse-interfaces.md`).
- Requires a fresh DQM schema; 0.1 history is preserved in its original schema and is not migrated. Later upgrades use verified additive migrations.
- Replaces checkpoint replay and whole-row merges with frozen inputs, transactional lifecycle-only apply, receipts and generation fencing. Reviewer annotations and versions are preserved; closure workflow is captured atomically.
- Adds row owners, conflict warnings, priority/criticality, visible tags, independent review verdicts, recurrence context, timeline and durable payload-change events.
- Adds known missed-issue reporting and warehouse health views with explicit populations, denominators and local offline snapshots.
- Retires reconciliation lookback and unconditional BigQuery raw-log expiration. Receipt-aware retention preserves unprocessed evidence; events have separate opt-in retention.
- Emits native BigQuery table/view grants without revoking unrelated access, and normalizes timestamp arithmetic for portable health and recovery filters.
- Schema-only (`--empty`) runs require initialized current tables, leave tracking data unchanged, and keep the public views serving real data.
- BigQuery adapter parity remains gated on credentialed lifecycle/concurrency tests.
- Capture copies each stored-failure table once and rejects it as `collection_error` when its row count differs from dbt's `result.failures`, including passing tests. Equal counts do not prove provenance; captures sharing failure tables must remain serialized.
- The review app verifies each sync before replacing its cache: setup must be ready, the control generation unchanged across the read, and the issue view's row count must match the tracking table. Inconsistent or unavailable data keeps the cache and pending edits; a verified empty result replaces it.
- Postgres app connections use a lock timeout (`--lock-timeout`, default 5 seconds). Writes blocked by a running reconciliation, and BigQuery transaction conflicts, report the warehouse as busy without changing data. Sync and setup failures show a short message with technical details on request.
- The app reads a Postgres profile's password from either `pass` or `password`, as dbt does.
- `dqm_reconciliation_control` can be listed in `dbt_dqm_table_grants`; reviewers need `select` on it for verified syncs. The documented Postgres reviewer grant set is proven by an acceptance test with separate non-superuser runner and reviewer roles.
- Tracking-table grants are applied only by reconciliation and capture, not also by the parallel app-table models, so BigQuery no longer hits IAM "concurrent policy changes" errors during a package build.
- `dbt_dqm_event_payloads` (`full` default, `changed_columns`, `none`) controls payload retention in events, adding `payload_mode`, `changed_columns` and payload digests. Migration `0002_event_payload_mode` upgrades existing 0.2 schemas in place.
- Migration `0003_app_change_staging` creates the BigQuery app's staging table during setup; the app no longer issues DDL when applying edits, so BigQuery reviewers need no table-create rights on the DQM dataset.
- Migration `0004_app_staging_safety` isolates upload attempts, supplies warehouse timestamps and bounds failed staging retention to 24 hours. Annotation idempotency is checked inside apply's transaction. Run migrations before restarting reviewer apps; old/new staging clients must not overlap.
- App sync enforces 50,000 issues and 128 MiB of serialized data, streams downloads and atomically replaces issue/Health snapshots. Oversized syncs preserve the cache and pending edits; an older oversized cache exposes bounded pending-edit pages.
- Add a separate restricted-account BigQuery QA test that preserves the pre-created QA dataset; it does not reuse destructive acceptance fixtures.
- BigQuery reconciliation prunes observation partitions older than the run's evidence (−63% bytes at 1M occurrences), and the replay considers only Active issues of tests with new evidence plus untracked tests (Postgres steady reconcile 5.1 s → 2.4 s at 1M occurrences). Method and results: `docs/scale.md`.
- Event values have their own retention and, with the default `full` payload mode and no `dbt_dqm_event_retention_days`, are kept indefinitely after raw logs are pruned.
- Reconciliation planning no longer degrades to nested loops on Postgres, and the replay reads only Active rows plus the history of affected identities (see `docs/scale.md`).


## 0.1.1 (development history, never released; incorporated into 0.2.0a1)

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
