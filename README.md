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

## Choose a starting path

- [Try the Postgres demo](docs/quickstart-postgres.md) with Docker, uv, and disposable local data.
- [Try the BigQuery demo](docs/quickstart-bigquery.md) with dbt 2, an authorized Google Cloud
  project, and a disposable dataset.
- [Add dbt-dqm to an existing project](docs/quickstart-existing-project.md). Track one test in a
  development target and keep its original check in ordinary dbt CI.

There are two installations. The dbt package supplies the warehouse models and macros. Install
Git revision `v0.2.5` with `dbt deps`. The Python package `dbt-dqm==0.2.5` supplies the command
and review app. See [supported configurations](docs/supported-configurations.md) for engine
versions and adapter extras.

## Review the results

Run `dbt-dqm app` to open the local browser app. Sync the saved issues, assign owners, record a
review verdict or notes, and use **Apply and sync** to submit edits. You can stage bulk changes
and export filtered saved issues across pages as CSV or Excel. The app uses the same dbt project,
profile, target, and naming variables as the command. See the [review app guide](docs/review-app.md)
for its controls and limits.

## More guidance

- [Documentation index](docs/index.md)
- [Concepts and issue lifecycle](docs/concepts.md)
- [CLI reference](docs/reference-cli.md)
- [0.2.5 release notes](docs/release-notes-0.2.5.md)

dbt-dqm is Apache-2.0 open source. See the [license](LICENSE.md).
