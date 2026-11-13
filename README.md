# dbt-dqm

dbt tests tell you when data is wrong. When the same test runs again, it can be hard to tell which
records are still affected, which ones returned, and what someone has already done about them.
**dbt-dqm turns selected failed rows into persistent, reviewable issues.**

Choose the columns that identify a record, such as `customer_id`. dbt-dqm uses them to recognize
the same issue across runs, even when other values change. It keeps the captured row, tracks when
the issue appears, passes, or recurs, and preserves owner assignments and review notes. A local
browser app lets reviewers triage the resulting backlog; CSV and Excel exports let you share it.

Use it for dbt data tests whose failures need follow-up at the record level. Your regular dbt
workflow stays separate: the dedicated `dbt-dqm run` command enables and runs the tests you mark
for DQM, captures their results, and updates the issue history.

## Get started

Start with an existing dbt project, a configured profile, and a supported warehouse adapter.
dbt-dqm has **two installations**: the dbt package supplies warehouse models and macros; the
Python package supplies the `dbt-dqm` command and local review app. Use matching `0.2.5`
versions. See the compatibility record below for tested engines and warehouses.
The examples below install the published release. A local checkout can also be used for development.

1. Add the dbt package to your project's `packages.yml`, then install it:

   ```yaml
   packages:
     - git: https://github.com/a-adamovich/dbt-dqm.git
       revision: v0.2.5
   ```

   ```bash
   dbt deps
   ```

   For development, replace the Git entry with `local: /path/to/dbt-dqm`.

2. Choose one engine/warehouse option in a Python environment, then install the command
   and its warehouse client separately:

   ```bash
   # BigQuery with dbt v2
   python -m pip install 'dbt==2.0.11'
   python -m pip install 'dbt-dqm[bigquery]==0.2.5'
   # Postgres with dbt v1
   python -m pip install 'dbt-core==1.12.6' 'dbt-postgres==1.11.0'
   python -m pip install 'dbt-dqm[postgres]==0.2.5'
   ```

   The Python command supports Python 3.11–3.14. dbt Core 1.11–1.12 supports both
   BigQuery and Postgres; the new dbt 2.0.11 path targets BigQuery. The extras install
   app warehouse clients and do not install dbt. Choose another engine with
   `--dbt-executable /path/to/dbt` or `DBT_DQM_DBT_EXECUTABLE`. See
   [installation and compatibility](docs/operations.md#installation-and-compatibility)
   for adapter-specific requirements.

3. Add a singular test such as `tests/customers_missing_email.sql`. Each returned row is a
   failure. `granularity` names the columns that identify the issue; the other columns are
   captured as values that can change over time.

   ```sql
   {{ config(
       enabled=var('dqm_enabled', false),
       store_failures=true,
       limit=100,
       meta={'dbt_dqm': {
           'granularity': ['customer_id'],
           'owner': 'customer_data'
       }}
   ) }}

   select customer_id, email
   from {{ ref('customers') }}
   where email is null
   ```

   Set retention periods for the captured execution and event history in `dbt_project.yml`:

   ```yaml
   vars:
     dbt_dqm_retention_days: 30
     dbt_dqm_event_retention_days: 90
   ```

   The test is disabled during ordinary dbt commands. `dbt-dqm run` enables metadata-marked DQM
   tests for its own workflow. You can also assign owners from a failure-row column; see
   [test configuration](docs/using-dbt-dqm.md#configure-tests).

   To track an existing generic test, put the same configuration under its `config:`. **The
   `enabled` line turns the test off in ordinary `dbt test` and `dbt build` runs.** If it must keep
   running there, leave it unchanged and add a separately named copy for DQM:

   ```yaml
   columns:
     - name: email
       data_tests:
         - not_null                  # the existing test, unchanged
         - not_null:
             name: dqm_email_not_null
             config:
               enabled: "{{ var('dqm_enabled', false) }}"
               store_failures: true
               limit: 100
               meta: {dbt_dqm: {granularity: ['customer_id'], owner: customer_data}}
   ```

4. Build the upstream models as you normally would, then run the DQM workflow:

   ```bash
   dbt-dqm run
   ```

   The first run creates the tracking relations. Later runs update existing issues, archive
   issues whose tests conclusively pass, and recognize recurrences. A failing dbt test can make
   this command exit `1` **after** its failure evidence has been captured and reconciled. The
   completion line reports test outcomes, captured issues, and collections that reached their
   limit. Add `--summary` to see changes and active issues by owner. See
   [running DQM](docs/using-dbt-dqm.md#run-and-interpret-results) for selection, deferral,
   summaries, and limit behavior.

5. See an issue pass and recur. In a development target, use a synthetic row you control and the
   same selector on every run, so the summary counts come from that test alone:

   ```bash
   dbt-dqm run --select dqm_email_not_null --summary   # bad row present: OPENED (NEW) 1, exit 1
   dbt-dqm run --select dqm_email_not_null --summary   # row fixed and rebuilt: ARCHIVED 1, exit 0
   dbt-dqm run --select dqm_email_not_null --summary   # row restored and rebuilt: RECURRING 1, exit 1
   ```

   The recurrence is a new occurrence of the same record, and the earlier review is shown with it.
   The [pilot walkthrough](docs/pilot-walkthrough.md) has the complete steps, including review.

## Review the results

Run `dbt-dqm app` to open the local browser app. Sync the saved issues, assign owners, record a
review verdict or notes, and use **Apply and sync** to submit edits. You can stage bulk changes
and export filtered saved issues across pages as CSV or Excel. The app uses the same dbt project,
profile, target, and naming variables as the command. See the [review app guide](docs/review-app.md)
for its controls and limits.

## More guidance

- [Pilot walkthrough](docs/pilot-walkthrough.md): track one existing test, review its first
  issue, and see it pass and recur.
- [Configure tests and run DQM](docs/using-dbt-dqm.md): identity, ownership, collection limits,
  CLI options, lifecycle, and owner summaries.
- [Operate and upgrade](docs/operations.md): installation details, naming, authentication,
  maintenance, resets, upgrades, and troubleshooting.
- [Try the demo projects](docs/operations.md#demo-projects) or inspect the
  [warehouse contract](docs/warehouse-interfaces.md) and [scale measurements](docs/scale.md).
- [0.2.5 release notes](docs/release-notes-0.2.5.md) and [compatibility verification](docs/dbt-2-compatibility.md).

dbt-dqm is Apache-2.0 open source. See the [license](LICENSE.md).
