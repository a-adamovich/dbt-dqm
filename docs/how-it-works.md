# How a run works

This page follows one `dbt-dqm run` from the command line to the warehouse, then follows the
results into the review app and back. It continues the `customers_missing_email` example from
[Concepts](concepts.md). For the tables involved, read [Warehouse tables](warehouse-tables.md).
For every flag, read [Configure tests and run dbt-dqm](using-dbt-dqm.md).

## The short version

`dbt-dqm run` is a wrapper around ordinary dbt commands. It never imports dbt. Each step runs your
dbt executable as a separate program and reads its JSON log. A run does five things:

1. Checks the tracked tests and their output columns.
2. Creates or upgrades the dbt-dqm tables if needed.
3. Runs the tracked tests with a bounded test materialization.
4. Copies the failed rows into tracking tables.
5. Reconciles them into occurrences, rebuilds the public views and runs maintenance.

Your project files are never changed. Ordinary `dbt test` and `dbt build` keep using dbt's own test
materialization.

## One run, step by step

The runner calls dbt in this order. Source: [`runner.py`](../src/dbt_dqm_app/runner.py),
`Workflow._execute`.

| Step | dbt call | What it does |
| --- | --- | --- |
| 1 | `parse` | Builds the manifest. |
| 2 | `run-operation dbt_dqm.workflow_settings` | Resolves the dbt-dqm relation names, default limit, retention and payload settings. |
| 3 | `ls --resource-type test` | Applies your `--select` and `--exclude`, then keeps tests with `meta.dbt_dqm`. |
| 4 | none (Python) | Validates each selected test's configuration and stops on errors. |
| 5 | `compile` and `run-operation dbt_dqm.validate_outputs` | Compiles the selected tests and checks that every granularity column appears exactly once in each query's output. |
| 6 | (only with `--full-refresh`) | Shows the reset scope, asks for confirmation (or `--yes`) and resets this installation's tables. |
| 7 | `run-operation dbt_dqm.initialize_dqm` | Creates the schema and tables if missing and applies pending migrations. |
| 8 | `test --select …` | Runs the tracked tests through the bounded materialization. |
| 9 | `run-operation dbt_dqm.collect_results` | Captures executions and failed rows. |
| 10 | `run --select package:dbt_dqm` | Builds the app tables, reconciles in the `dqm_reconcile` model, then rebuilds the public views. |
| 11 | (only with `--summary`) | Prints changes and open work by owner. |
| 12 | `run-operation dbt_dqm.maintain_dqm` | Runs retention and cleanup. |

Every call passes `dqm_enabled: true` and a private workflow variable. On dbt 2, every call also
turns off dbt's service-backed state and execution reuse, so a run never reuses an earlier test
result as fresh evidence.

### Validate before touching the warehouse

Steps 1 to 5 change nothing in the warehouse. A misconfigured test, a missing granularity column
or an ambiguous column name stops the run before setup, and before a full refresh could delete
anything. A tracked test that isn't selected but is missing `store_failures` or `granularity` gets
a warning, so you hear about it before you select it.

### Run the tests in a temporary project

dbt only lets the **root** project override a built-in materialization such as `test`. dbt-dqm is
installed as a package, so it can't override the materialization in your project directly. For the
`test` step, the runner builds a temporary copy of your project:

- Your folders are symlinked (copied on Windows without symlink rights).
- `dbt_project.yml` is rewritten so relative paths still resolve to the original project.
- One generated macro forwards `materialization test` to dbt-dqm's implementation.
- Any test materialization in your own macros is hidden for this run only.

Test names and unique IDs stay the same, because the project name and relative paths don't change.
Large inputs, such as compiled SQL, travel as a project variable instead of on the command line,
because Windows and Linux limit command-line length. If you define your own test materialization
in a macro path of `.`, the runner refuses to start: it can't hide that file safely.

The bounded materialization runs only when three things hold: the run is dbt-dqm's workflow,
`dqm_enabled` is true, and the test has `meta.dbt_dqm`. It recreates the store-failures table
from your query, ordered by identity and then by the whole row, with the `limit` applied. Then
it computes the test status from that table the same way dbt does. Every other test, in this run
or any other dbt command, uses dbt's native materialization.

Source: [`runtime_project.py`](../src/dbt_dqm_app/runtime_project.py),
[`test_materialization.sql`](../macros/test_materialization.sql) and `get_limit_subquery_sql` in
[`workflow.sql`](../macros/workflow.sql).

### Capture the exact invocation

Capture reads the results of step 8 only. For each tracked test it writes one row to
`dqm_test_executions` and sets a collection status:

- **Failing test.** It reads the store-failures table once into a temporary stage, hashes each
  row's granularity values into `unique_id`, keeps all columns as `record_values_json`, collapses
  duplicates, and writes one row per identity to `dqm_issue_observations`. If the row count reached
  the limit, the execution is `limit_reached`.
- **Test with zero reported failures.** Its store-failures table must be empty.
- **Bad configuration, errored or skipped test.** The execution is `configuration_error` or
  `inconclusive`. These never count as evidence.

**Why a row-count mismatch is a collection error.** Capture compares the rows in the store-failures
table with the failure count dbt reported. If they differ, the evidence is inconsistent, so the
execution becomes `collection_error` and never counts as a pass. One cause is another run
overwriting the table. Another is a test with `warn_if` or `error_if` thresholds: when it passes
under its threshold, dbt Core reports zero failures even though the query returned rows. Such a
test records a collection error each time it passes that way, so it can't close issues.

**Why a collection error can leave earlier evidence in place.** Capture commits each failing test
separately. If capture breaks partway through, the tests already processed keep their evidence,
and the rest stay `pending`. The run still reconciles what was committed, then exits `2`.

**Why two runs must not share store-failures tables.** Equal row counts don't prove the rows came
from this run. Two runs writing the same failure tables at the same time can mix evidence. Run them
one after another.

Source: [`capture_test_results.sql`](../macros/capture_test_results.sql).

### Reconcile: turn evidence into occurrences

Reconciliation runs in the hooks of the `dqm_reconcile` model. The model itself is a plain view
over `dqm_issue_occurrences`. Its pre-hook and post-hook do the work. Source:
[`reconciliation.sql`](../macros/reconciliation.sql) and [`replay.sql`](../macros/replay.sql).

1. **Guard.** On BigQuery, the run refuses to start if another reconciliation started within the
   last `dbt_dqm_reconcile_timeout_minutes` (60 by default). On Postgres, it locks the occurrence
   table instead, so other runs wait.
2. **Freeze.** It records the current generation and freezes the list of executions it will
   process. That list holds every usable execution of a currently tracked test that no earlier
   reconciliation has processed.
3. **Late-evidence check.** If a frozen execution is older than one this test already processed,
   the run stops before changing anything (see [When something goes wrong](#when-something-goes-wrong)).
4. **Replay.** For each test and identity it orders the frozen executions by capture time and
   marks each one failing or absent. Consecutive failures form one occurrence. An absence in
   complete evidence closes it. A failure after an absence starts the next one. The result goes
   into a stage table.
5. **Apply.** In one transaction, it checks that the generation is unchanged since the freeze,
   upserts occurrences without touching reviewer fields, inserts events, records receipts for the
   frozen executions, increases the generation and marks the run completed.

Replay processes evidence in capture order, so a reconciliation that failed earlier is caught up
by the next run without skipping or reordering anything.

**Selection versus tracking.** `--select` and `--exclude` decide which tests execute in this run
(steps 3 to 9). They don't limit reconciliation. The list of tracked tests comes from the manifest
of step 10, which includes every enabled test with `meta.dbt_dqm`, whatever you selected. The
freeze then takes every unprocessed usable execution of any tracked test. So a run that selects
only one test can still apply evidence that an earlier run captured for another test and never
reconciled, for example because that reconciliation failed. A tracked test with no unprocessed
evidence keeps its open issues as they are. A test that is gone from the manifest (deleted,
renamed, disabled, or without `meta.dbt_dqm`) is no longer tracked, and its open issues close as
`TEST_REMOVED`.

**What counts as evidence.** Only executions with a `pass`, `warn` or `fail` status and a
`success`, `not_applicable` or `limit_reached` collection are frozen. A `limit_reached` execution
can open and update the issues it saw, but identities it didn't return are ignored, including old
identities after a grain change. So only complete, conclusive results can close an issue as
`Passed` or `IDENTITY_CHANGED`. `TEST_REMOVED` is the exception: it depends only on whether the
test is still in the manifest.

**Why BigQuery stages are real tables.** On BigQuery, each phase is a separate script, so the
change set must outlive the pre-hook. It goes into a table named after the run's invocation ID,
which expires after 24 hours. Two runs can't apply each other's stage. Postgres uses a temporary
table in the same transaction instead.

### Maintenance

After reconciliation, maintenance prunes raw evidence and events if you set retention for them. On
BigQuery it always deletes expired app staging rows, drops old stage tables and prunes maintenance
log rows older than 30 days ([Retention](warehouse-tables.md#retention) has the details). A failing
maintenance step is
logged and retried next time. It never fails the run. `dqm_maintenance_health` shows each step's
state. Source: [`maintenance.sql`](../macros/maintenance.sql).

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Every step succeeded and no tracked test failed. Tests at `warn` severity still capture their rows and open issues. |
| `1` | At least one tracked test failed, **and** its evidence was captured and reconciled. |
| `2` | A step failed, a test errored or was skipped, or capture had a problem. Read the message. Evidence committed before the problem is still reconciled when the run gets that far. |

**Exit `1` is expected.** A failing test is what dbt-dqm exists to record. The command exits `1`
only after the failed rows are safely in the warehouse, so a scheduler sees a failure without losing
the evidence. Treat `1` as "issues found" and `2` as "the run needs attention".

The final line summarizes the run, for example
`DQM complete: 1 tests (1 fail), 2 captured issues, 0 collections reached the limit, 14.2s.` If a
collection reached its limit, a warning names the test. The runner also writes
`dqm_workflow_metrics.json` to the target path, even when the run fails.

## How a run reaches the app

The app is a single-user Streamlit page that `dbt-dqm app` starts on `127.0.0.1`. It reads the
warehouse directly with the BigQuery or Postgres client. It never reads dbt artifacts for issue
data.

### Finding the same tables

Your naming macros (`generate_schema_name`, `generate_alias_name`) and `dbt_dqm_schema` decide
where the dbt-dqm tables live. The app asks dbt for the resolved names in a separate process, so it
always addresses the same installation as `run`. **Pass the same naming `--vars` to `run`, `app`
and `workspace`.** Different values point the app at a different installation, or at none.

### Sync

**Sync** runs `dbt debug` and `dbt parse` with your selected dbt, checks that the tables exist and
carry the `dqm-full-v1` marker, then reads `dqm_all_issues`: every Active issue plus archived issues
from the last 90 days (`--archive-cache-days`). The rows replace the local snapshot in your
workspace. Source: `fetch_issues` in [`warehouse.py`](../src/dbt_dqm_app/warehouse.py).

Two checks protect the snapshot:

- The app reads the generation before and after. If a reconciliation finished during the read,
  it keeps the cached data and asks you to sync again.
- It compares the view's row count with the occurrence table. A stale or redefined view can't
  silently empty your cache.

### Browse modes

- `snapshot` keeps every synced issue in the local SQLite file.
- `warehouse` reads one page at a time from the warehouse.
- `auto` (the default) syncs locally while the issues fit under 50,000 rows and 128 MiB, and
  switches to warehouse pages above that.

On BigQuery, a search in warehouse mode first runs as a dry run. If the estimate exceeds
`--max-search-gib` (1 GiB by default), it doesn't run. If it runs, BigQuery enforces the same cap
with `maximum_bytes_billed`, so a low estimate can't cost more than the cap.

The local workspace lives in your user data folder and holds captured row values. `dbt-dqm
workspace info` shows where, and `dbt-dqm workspace purge` deletes it.
[Review issues in the local app](review-app.md) covers the app itself.

## Review edits

### Stage, then apply

Edits in the app are first saved as pending changes in the local workspace. Each pending change
remembers the field's old value, the occurrence's `annotation_version` at the time, and the
installation it belongs to. Nothing reaches the warehouse until you click **Apply and sync**.

On apply, the app groups the pending changes into one batch whose ID is derived from the changes
themselves. Applying the same batch twice has no further effect, so retrying after a network
failure is safe.

- **BigQuery.** The app uploads the batch into the package-owned `dqm_app_change_staging` table,
  then runs one transaction. It checks the batch is complete, belongs to this installation, is less
  than 24 hours old, and that every `annotation_version` still matches. It then writes one audit row
  per field to `dqm_annotation_changes`, updates the occurrences, increases their
  `annotation_version`, and removes the staged rows. Reviewers therefore need row access to that
  staging table, not permission to create tables.
- **Postgres.** The app locks the affected occurrences, checks versions, writes the audit rows and
  updates the occurrences in one transaction.

Then the app syncs again. The public views read the occurrence table directly, so the change is
visible at once to anyone who queries them.

`changed_by` in the audit table is the operating-system user name on the machine that ran the app.
It isn't an authenticated warehouse identity.

### Conflicts

If someone else changed an occurrence after you staged an edit, the version check fails and
nothing in that batch is written. After the next sync, the app shows your edits as conflicts. You
choose **Discard conflicts** or **Allow overwrite**, which rebases your edits on the newer values.
It never overwrites silently.

A workspace with pending edits refuses to sync from a different installation, for example after a
full refresh. Apply or discard your edits first, or purge the workspace.

### What carries over between runs

Runs never write reviewer fields on an existing occurrence. What you see depends on what the
record does next:

| What happens to the record | Workflow, verdict, owner, notes, ticket, call to action |
| --- | --- |
| Still failing | Kept as they are on the same occurrence. |
| Passes (archived) | Kept on the archived occurrence. `workflow_status_at_close` records the workflow at that moment. |
| Fails again after passing | The new occurrence starts with workflow `NEW`, verdict `UNREVIEWED`, empty notes and ticket, and owner and call to action from the test. The previous occurrence's verdict, owner, ticket, notes and closing workflow appear in the `previous_*` columns of `dqm_current_issues` and `dqm_all_issues`. |

## When something goes wrong

These are normal operational situations, not upgrade problems. The macros named here are part of
the product. [Recovery and retention](warehouse-interfaces.md#recovery-and-retention) has the exact
commands.

- **"Another DQM reconciliation is active."** On BigQuery, a run whose client died stays `started`
  until the timeout passes. Wait, or abandon it with `dqm_abandon_runs`.
- **"Late DQM evidence."** An older capture was frozen after a newer one for the same test had
  already been applied. Applying it would rewrite history, so reconciliation stops. Review it, then
  acknowledge the gap with `dqm_skip_late_evidence` and a reason. That writes an audited
  `EVIDENCE_SKIPPED` event.
- **Leftover stage tables.** Maintenance drops them. `dqm_cleanup_stages` does it on demand.

Upgrade and incident-repair code is a separate topic, covered in [Legacy code](legacy.md).
