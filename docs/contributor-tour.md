# Contributor source tour

This tour follows one issue, customer `C-1` failing `customers_missing_email` from
[Concepts](concepts.md), through the source code. It names the file and the function or macro at
each stop, and the tests that cover it. Read [How a run works](how-it-works.md) first for the
behaviour. This page shows where that behaviour lives.

## The layout

| Path | What it is |
| --- | --- |
| [`dbt_project.yml`](../dbt_project.yml), [`macros/`](../macros), [`models/`](../models) | The dbt package `dbt_dqm`. |
| [`tests/assert_*.sql`](../tests) | Package invariants as singular dbt tests. They run inside the demo projects. |
| [`src/dbt_dqm_app/`](../src/dbt_dqm_app) | The Python package `dbt-dqm`: the CLI, the runner and the Streamlit app. |
| [`tests/test_*.py`](../tests) | Python unit tests. They render SQL and run the CLI against fakes, without a warehouse. |
| [`integration_tests/demo_postgres/`](../integration_tests/demo_postgres), [`integration_tests/demo_bigquery/`](../integration_tests/demo_bigquery) | Demo dbt projects with scenario seeds. |
| [`integration_tests/acceptance/`](../integration_tests/acceptance) | Acceptance tests that run the demos against a real warehouse. |
| [`scripts/`](../scripts) | CI, release and evidence helpers, and the legacy #105 repair. |

The Python side never imports dbt. It runs your dbt executable as a subprocess and reads results
from dbt's JSON log and artifacts. That keeps one code path for dbt Core 1.x and dbt 2.

## 1. The command

[`cli.py`](../src/dbt_dqm_app/cli.py) defines `run`, `app` and `workspace`. For `run`, `launch` in
[`runner.py`](../src/dbt_dqm_app/runner.py) writes the arguments to a temporary `request.json` and
starts `python -m dbt_dqm_app.runner` as a child process. Errors cross back only as text and an exit
code.

Tests: `tests/test_cli.py`, `tests/test_runner.py`.

## 2. Configuration and the dbt executable

`load_config` in [`config.py`](../src/dbt_dqm_app/config.py) reads `dbt_project.yml` and
`profiles.yml` itself, rendering `env_var` and `var` in a sandboxed Jinja environment. It accepts
only `bigquery` and `postgres` profiles.

[`dbt_cli.py`](../src/dbt_dqm_app/dbt_cli.py) holds `DbtCLI`. `resolve_dbt_executable` picks the
executable from `--dbt-executable`, then `DBT_DQM_DBT_EXECUTABLE`, then the `dbt` next to the
current Python, then `PATH`. `validate` enforces the supported versions and refuses dbt 2 with
anything but BigQuery. `invoke` adds JSON logging and, on dbt 2, the flags that turn off
service-backed state and execution reuse. It also turns on the temporary project for `test` (see
stop 4). dbt-dqm's own results come back as log lines prefixed `DQM_SETTINGS=`, `DQM_COLUMNS=`,
`DQM_COLLECTION_ERRORS=`, `DQM_COLLECTION_SUMMARY=` and `DQM_MAINTENANCE=`, which
`Workflow.event` parses.

Tests: `tests/test_config.py`, `tests/test_dbt_cli.py`, `tests/test_dbt_v2.py` (real dbt 2
executables, offline).

## 3. Selection and validation

`Workflow._execute` in `runner.py` is the whole run in one method, with the step order from
[How a run works](how-it-works.md#one-run-step-by-step). `validate_test` checks each selected test's
configuration. The `validate_outputs` macro in
[`test_materialization.sql`](../macros/test_materialization.sql) compiles each query's output
columns and checks that every granularity column matches exactly one of them.

Tests: `tests/test_runner.py`.

## 4. The bounded test materialization

`test_project` in [`runtime_project.py`](../src/dbt_dqm_app/runtime_project.py) builds the temporary
root project with one forwarding `materialization test` macro. The package's override in
`test_materialization.sql` chooses `execute_dqm_test` only for tracked tests inside the workflow.
`get_limit_subquery_sql` in [`workflow.sql`](../macros/workflow.sql) wraps the test query, ordering
rows by identity hash and then row JSON before the limit.

Tests: `tests/test_runtime_project.py`, and the acceptance case
`test_dedicated_run_preserves_an_existing_root_test_materialization`.

### Engine-specific paths

The temporary test project keeps the original project name and relative source paths.
It links sources and installed packages, with private copies when Windows denies symlinks.
Artifact and state paths stay anchored to the original project directory. Internal SQL,
result payloads, and column maps travel through the projected configuration as literal
JSON, which keeps them out of command-line arguments.

The dbt 2 preflight uses a separate projection without global hooks. The package's
empty-mode guard lives in model pre-hooks because dbt 2 can execute global hooks
during compilation. The Core-only offline compile helper uses a shim to avoid opening
an unused lazy BigQuery connection on close. See the [offline checks](ci-measurements.md#run-the-dbt-2-checks-locally).

dbt 2 does not expose the manifest metadata needed to check for maintenance macro
overrides. It runs maintenance steps normally even if the experimental shortcut is enabled.
On Core, the probes can skip eligible jobs when the runner can establish default behavior.

## 5. Capture: `C-1` becomes an observation

`collect_results` in `workflow.sql` receives the results of the `test` step and runs the SQL that
`capture_test_results` in [`capture_test_results.sql`](../macros/capture_test_results.sql) builds:

- One `dqm_test_executions` row per tracked test, inserted as `pending`.
- For a failing test, a temporary `dqm_capture_stage` read once from the store-failures table, where
  `C-1`'s `customer_id` is normalized and hashed by `canonical_identity_string` and `sha256_hex`
  (in [`adapters.sql`](../macros/adapters.sql)), and its full row is serialized by
  `json_object_string`.
- One transaction per failing test that inserts the deduplicated observations and sets the
  collection status.

The identity format is fixed by golden vectors in `tests/assert_identity_hash_vectors.sql`, which
must give the same SHA-256 on every adapter.

Tests: `tests/test_protocol_render.py`, `tests/assert_collection_evidence_guardrails.sql`, and the
acceptance case `test_capture_rejects_failure_tables_changed_before_capture`.

## 6. Reconciliation: the observation becomes an occurrence

`dbt run --select package:dbt_dqm` builds `dqm_reconcile`
([`models/dqm_reconcile.sql`](../models/dqm_reconcile.sql)). Its hooks, set in `dbt_project.yml`,
call macros in [`reconciliation.sql`](../macros/reconciliation.sql):

- `reconcile_lock` (Postgres advisory lock).
- `reconcile_pre`: setup, the concurrency guard, the freeze into `dqm_reconciliation_runs` and
  `dqm_reconciliation_inputs`, the late-evidence assertion, and the stage.
- `apply_reconciliation`: the generation check, the occurrence upsert that skips reviewer columns,
  events, receipts, the per-test high-water mark, and the generation increase.

The change set itself is `reconcile_change_set_sql` in [`replay.sql`](../macros/replay.sql). Read its
CTEs in order: `active_existing` and `candidate_identities` choose what can change,
`execution_identity_states` marks each frozen execution failing or absent for each identity (and drops
absences from `limit_reached` executions), `states_grouped` numbers the episodes,
`episode_occurrences` turns episodes into occurrence rows with close reasons, `orphaned_occurrences`
handles `TEST_REMOVED`, and `transition_events` derives events. For `C-1` on its first run, that
yields one occurrence with `occurrence_number = 1` and one `APPEARED` event.

The package invariants `assert_at_most_one_active_occurrence.sql`, `assert_occurrence_id_unique.sql`,
`assert_event_id_unique.sql` and `assert_receipt_unique.sql` check the result after every demo run.

Tests: `tests/test_protocol_render.py`, and the acceptance cases
`test_lifecycle_annotations_history_and_health`, `test_batched_replay_rollback_and_empty`,
`test_reconciliation_serializes_and_reviewer_edit_survives` and
`test_late_skip_cleanup_and_future_migration`.

## 7. Schema, setup and the public views

[`table_schemas.sql`](../macros/table_schemas.sql) declares the internal tables and their keys.
[`schema_management.sql`](../macros/schema_management.sql) holds `setup_sql`, the migration registry
and the grant helpers. [`helpers.sql`](../macros/helpers.sql) resolves every relation through
`dqm_relation`, using the consumer's naming macros, and `relation_contract` lists them all.

The views in [`models/`](../models) are thin. `dqm_current_issues` and `dqm_all_issues` both call
`issues_with_history` in [`history.sql`](../macros/history.sql), which joins each occurrence to the
previous one. The health views use [`health.sql`](../macros/health.sql). Maintenance lives in
[`maintenance.sql`](../macros/maintenance.sql) and the recovery macros in
[`recovery.sql`](../macros/recovery.sql).

Tests: `tests/test_docs_consistency.py` (every model is cataloged in `models/schema.yml`),
`tests/test_maintenance_health.py`, and the acceptance cases for migrations and maintenance.

## 8. The app: `C-1` reaches a reviewer

[`streamlit_app.py`](../src/dbt_dqm_app/streamlit_app.py) is the page. `run_app` drives it, and long
operations run in a process pool (`_submit_sync`, `_submit_apply`).

- [`relations.py`](../src/dbt_dqm_app/relations.py) resolves physical names by running dbt in a
  child process (`run-operation dbt_dqm.resolved_relations` on dbt 2, `compile --inline` on dbt 1)
  and caches the answer.
- [`warehouse.py`](../src/dbt_dqm_app/warehouse.py) does all warehouse access with the native
  BigQuery and psycopg2 clients: `validate_dbt` and `check_required_relations` before a sync,
  `fetch_issues` for the snapshot, `browse_page` for warehouse paging, `apply_patches` with
  `_apply_bigquery_patches` and `_apply_postgres_patches` for review edits.
- [`store.py`](../src/dbt_dqm_app/store.py) is the SQLite workspace: the snapshot, pending changes,
  `drifted_patches` for conflicts, and the installation check.
- [`browse.py`](../src/dbt_dqm_app/browse.py) builds the parameterized browse SQL, and
  [`exports.py`](../src/dbt_dqm_app/exports.py) writes CSV and Excel files.

Tests: `tests/test_relations.py`, `tests/test_sync_and_errors.py`, `tests/test_annotation_staging.py`,
`tests/test_review_actions.py`, `tests/test_store.py`, `tests/test_browse.py`,
`tests/test_browse_warehouse.py`, `tests/test_exports.py`, `tests/test_ui.py`, and the acceptance cases
`test_app_verdict_audit_missed_submission_and_shared_dependencies` and
`test_warehouse_browsing_matches_the_snapshot_and_sees_generation_changes`.

## Where BigQuery and Postgres differ

Issue identity, lifecycle transitions, and granularity rules should behave the same on
BigQuery and Postgres. A difference in those results is a bug. Most SQL is shared, with
adapter-specific implementations in a few places:

- **Dispatched macros** in [`adapters.sql`](../macros/adapters.sql) and [`helpers.sql`](../macros/helpers.sql):
  string literals, SHA-256, JSON building, insert-if-missing, the high-water upsert and changed-key
  detection. Maintenance steps are dispatched too.
- **Inline `target.type` branches** in capture (transaction shape and the temporary stage),
  reconciliation (lock versus active-run check, temporary versus expiring stage table), setup
  (migration transaction versus lease) and the ordering JSON in `get_limit_subquery_sql`.
- **Materializations** in [`materializations.sql`](../macros/materializations.sql): `dqm_entry_view`
  replaces the Postgres `dqm_reconcile` view in place inside the lock, and `dqm_app_table` keeps
  the BigQuery app tables uncollated and never replaces a populated one.
- **Collation**: on BigQuery, every string comparison strips collation (`uncollated_string`), and
  tables are created with `default collate ''`.
- **Python clients**: separate BigQuery and Postgres code in `warehouse.py` and
  [`refresh.py`](../src/dbt_dqm_app/refresh.py).

An unsupported adapter fails at compile time with `unsupported_adapter_error`.

## Running the checks

[CONTRIBUTING.md](../CONTRIBUTING.md) has the commands. In short: Ruff and the unit suite on every
change, and the eight-case Postgres core tier against a disposable database when macros, models or
dbt execution change. [CI tiers and validation](ci-measurements.md) explains what each CI tier
covers. A passing compile or unit test is not evidence that BigQuery behaves the same way at
runtime: only the credentialed BigQuery workflows show that.
