# dbt-dqm 0.2.0

The first stable 0.2 release packages the behavior validated through 0.2.0a8. It tracks dbt
test failures as persistent issues, preserves reviewer annotations across runs, and provides a
local review app with bulk edits and CSV/Excel exports. `run --summary` reports issues opened,
reopened and archived by the current reconciliation, alongside the active workload by owner.

Upgrade both the Python app and the dbt package together. From 0.2.0a7 or 0.2.0a8, no warehouse
migration or full refresh is required. Earlier alphas may require a fresh, isolated namespace;
read the [migration instructions](https://github.com/a-adamovich/dbt-dqm/blob/v0.2.0/README.md#migrating-existing-tests).

Install the Python app with `pip install 'dbt-dqm[bigquery]==0.2.0'` or
`pip install 'dbt-dqm[postgres]==0.2.0'`. Until dbt Hub registration is complete, install the
dbt package with this `packages.yml` entry and run `dbt deps`:

```yaml
packages:
  - git: "https://github.com/a-adamovich/dbt-dqm.git"
    revision: v0.2.0
```

Supported runtimes: Python 3.11–3.14, dbt Core 1.11–1.12, dbt-bigquery 1.11–1.12, and
dbt-postgres 1.11 on Postgres 14 or 16. The app pins dbt-core 1.12.5 and its BigQuery extra
pins dbt-bigquery 1.12.1. The [README](https://github.com/a-adamovich/dbt-dqm/blob/v0.2.0/README.md)
documents operating limits and the separate app/package installation.
