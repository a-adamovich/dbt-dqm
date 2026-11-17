# Supported configurations

## Engines and platforms

| Engine | Warehouse | Adapter and evidence |
| --- | --- | --- |
| dbt Core 1.11–1.12 | Postgres 14 and 16 | `dbt-postgres` 1.11.0. Full validation covers the Core 1.11.0 lower bound. Enable `pgcrypto` in the target database. |
| dbt Core 1.11–1.12 | BigQuery | `dbt-bigquery`. CI uses 1.12.1 and the credentialed release tier runs a 9-of-33-case Core smoke selection. |
| dbt 2 | BigQuery | Bundled adapter. The CLI accepts `>=2.0.9,<3`. Offline checks use 2.0.9 and 2.0.11. The live-validated and production pin is 2.0.11. |

The production configuration for this release is dbt 2.0.11 on BigQuery `US` with dbt-dqm 0.2.5. dbt 2 support is limited to BigQuery. DuckDB, ClickHouse, Snowflake, and Databricks are outside this release's support.

The Python app supports 3.11–3.14. Use a Python version supported by the engine and adapter you install. CI includes Windows and macOS portability checks. On Windows, the runner uses private copies when symlink creation is denied. Large user-supplied argument lists still follow the platform's command-size limits. See [CI tiers](ci-measurements.md) and the [release checklist](release-checklist.md) for the coverage and gates.

## Installations

Install the dbt package at Git revision `v0.2.5` through `dbt deps`. Install the Python distribution separately as `dbt-dqm==0.2.5`. The `[postgres]` and `[bigquery]` extras provide the app's warehouse clients. Install your engine and any external dbt adapter separately. Use separate environments for Core and dbt 2 because both provide a `dbt` command. Select the intended one with `--dbt-executable` or `DBT_DQM_DBT_EXECUTABLE`.

See the [Postgres demo](quickstart-postgres.md), [BigQuery demo](quickstart-bigquery.md), or [existing-project pilot](quickstart-existing-project.md) for setup.

## Operating limits

- Serialize captures that share stored-failure tables. Equal counts alone cannot establish which invocation wrote those rows.
- BigQuery allows one reconciliation per resolved DQM installation at a time. Overlaps are detected and fail rather than waiting in a queue.
- Use a fresh schema when moving from 0.1 to 0.2. See [upgrading](operations.md#upgrading) for later migrations.
- Event payloads default to `full` with indefinite retention. Set a retention and event payload policy appropriate for the captured data.
- App cache ceilings measure serialized data. In auto mode, larger populations switch to warehouse browsing. On BigQuery, each 2,000-issue read and each summary is billed. See the [review app workflows](review-app.md) for browse modes and export limits.
- Maintenance Health is a monitoring approximation. Automated suites do not exercise live interactive OAuth sign-in for the app. See [optional maintenance](warehouse-interfaces.md#optional-maintenance).
