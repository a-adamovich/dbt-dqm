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
Python package supplies the `dbt-dqm` command and local review app. Use matching `0.2.2`
versions. This checkout is a release candidate; credentialed BigQuery verification on dbt 2.0.9
is still required before publication. The examples below install this checkout.

1. Add the dbt package to your project's `packages.yml`, then install it:

   ```yaml
   packages:
     - local: /path/to/dbt-dqm
   ```

   ```bash
   dbt deps
   ```

   After publication, use Git revision `v0.2.2` or the verified Hub listing.
   [Hub registration](https://github.com/dbt-labs/hubcap/pull/464) is pending.

2. From this checkout, install your dbt engine separately, then install the command and
   warehouse client used by the review app:

   ```bash
   # BigQuery with dbt v2
   python -m pip install 'dbt==2.0.9'
   python -m pip install -e '.[bigquery]'
   # Postgres with dbt v1
   python -m pip install 'dbt-core==1.12.5' 'dbt-postgres==1.11.0'
   python -m pip install -e '.[postgres]'
   ```

   The Python command supports Python 3.11–3.14. dbt Core 1.11–1.12 supports both
   BigQuery and Postgres; the new dbt 2.0.9 path targets BigQuery. The extras install
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
   tests for its own workflow. You can also configure generic tests and assign owners from a
   failure-row column; see [test configuration](docs/using-dbt-dqm.md#configure-tests).

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

## Review the results

Run `dbt-dqm app` to open the local browser app. Sync the saved issues, assign owners, record a
review verdict or notes, and use **Apply and sync** to submit edits. You can stage bulk changes
and export filtered saved issues across pages as CSV or Excel. The app uses the same dbt project,
profile, target, and naming variables as the command. See the [review app guide](docs/review-app.md)
for its controls and limits.

## More guidance

- [Configure tests and run DQM](docs/using-dbt-dqm.md): identity, ownership, collection limits,
  CLI options, lifecycle, and owner summaries.
- [Operate and upgrade](docs/operations.md): installation details, naming, authentication,
  maintenance, resets, upgrades, and troubleshooting.
- [Try the demo projects](docs/operations.md#demo-projects) or inspect the
  [warehouse contract](docs/warehouse-interfaces.md) and [scale measurements](docs/scale.md).
- [0.2.2 candidate notes](docs/release-notes-0.2.2.md) and [compatibility verification](docs/dbt-2-compatibility.md).
- [Stable 0.2.1 release notes](docs/release-notes-0.2.1.md) and
  [verification record](docs/release-0.2.1.md).

dbt-dqm is Apache-2.0 open source. See the [roadmap](docs/roadmap.md) and [license](LICENSE.md).
