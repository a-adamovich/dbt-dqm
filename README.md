# dbt-dqm

dbt-dqm is an open-source dbt package for persistent, row-level test issue tracking plus a
single-user local review app. The dbt package (`models/`, `macros/`) supports BigQuery and
Postgres; the supported dbt line is 1.11.x. The local review app (`dbt-dqm app`) supports both
package adapters — see [Local app](#local-app) below.

## Install the dbt package

dbt-dqm isn't on dbt Hub yet, so add it to your `packages.yml` as a git dependency, pinned to a
tag:

```yaml
packages:
  - git: "https://github.com/a-adamovich/dbt-dqm.git"
    revision: v0.1.0
```

Then run `dbt deps`. Note that `pip install dbt-dqm` (for the local review app CLI below) does
**not** also install the dbt package — the two are installed separately.

## Configure a tracked test

```sql
{{
  config(
    tags=['dqm'],
    store_failures=true,
    meta={'dbt_dqm': {
      'granularity': ['customer_id'],
      'capture_mode': 'allowlist',
      'context_columns': ['reason']
    }}
  )
}}

select customer_id, reason from {{ ref('customers') }} where is_invalid
```

Every failed-row query must return the configured granularity columns. Text identity comparisons
are case-insensitive. The safer default capture mode is `identity_only`; `allowlist` adds only
named context columns, while `full` explicitly opts into complete failed-row storage. There is no
column-name inference: `meta.dbt_dqm.granularity` is mandatory, and all configured columns must
exist in the test output.

## Run collection and reconciliation

Run these as separate native dbt steps. A failing test command must not prevent the second step.

```bash
dbt test --select tag:dqm
dbt build --select package:dbt_dqm
```

To run only reconciliation:

```bash
dbt run --select dbt_dqm.dqm_reconcile
```

The package publishes `dqm_current_issues`, `dqm_all_issues`, `dqm_test_executions`,
`dqm_issue_observations`, and `dqm_annotation_changes` in the target dataset. Set
`dbt_dqm_test_tag` or `dbt_dqm_schema` as project variables to override the defaults. With dbt's
standard schema naming, a `dbt_dqm_schema: governance` override produces
`<target_dataset>_governance`.

Optional `dbt_dqm_retention_days` and `dbt_dqm_reconcile_lookback_days` values must be positive
YAML integers. BigQuery applies partition expiration; on either adapter, explicit cleanup is
available with `dbt run-operation cleanup_dqm_logs --args '{retention_days: 90}'`.

## Supported warehouses

BigQuery and Postgres are both fully supported and covered by an integration demo each
(`integration_tests/demo_bigquery`, `integration_tests/demo_postgres`). Postgres requires the
`pgcrypto` extension for identity hashing: `create extension if not exists pgcrypto;` — dbt-dqm
doesn't create this automatically since `CREATE EXTENSION` commonly needs elevated privileges the
package's own role may not have. A third adapter is added by implementing the handful of
`adapter.dispatch()`-based macros in [`macros/adapters.sql`](macros/adapters.sql)
(`dual`, `sha256_hex`, `json_object_string`, `insert_new_rows`, `ensure_unique_index`,
`capture_tables_ddl`) for it — everything else in the package already uses dbt's own portable
cross-database macros (`dbt.type_string()`, `dbt.current_timestamp()`, `dbt.dateadd()`, ...) and
needs no adapter-specific code.

## Local app

```bash
dbt-dqm app \
  --project-dir /path/to/consumer-project \
  --profiles-dir ~/.dbt \
  --target demo
```

The app uses the selected dbt profile, validates it with `dbt debug`, and automatically
synchronizes at the start of each browser session. It uses dbt's rendered profile and generated
manifest, including `env_var()` authentication and custom `generate_schema_name` behavior. It
stores a project-and-target-specific failed-row snapshot plus field-level pending changes in an
owner-only SQLite workspace under the operating system's user-data directory; credentials are
never copied into it. Annotation updates use compare-and-set versions and return an explicit
conflict if another writer changed an occurrence. Inspect or permanently delete local data with:

```bash
dbt-dqm workspace info --project-dir /path/to/project --target demo
dbt-dqm workspace purge --project-dir /path/to/project --target demo
```

By default SQLite retains Active issues plus 90 days of Archived history. Pass
`--archive-cache-days 0` to cache Active issues only, or choose another non-negative window.

## BigQuery demo

The synthetic consumer is in `integration_tests/demo_bigquery`. Its profile should use a key outside
the repository, for example `~/.credentials/dbt-dqm/service-account.json`.
The demo's schema macro deliberately places stored failures in the same pre-created dataset so its
service account needs only BigQuery Job User on the project and BigQuery Data Editor on that dataset.

```bash
cd integration_tests/demo_bigquery
dbt deps
dbt run-operation reset_demo_dqm
dbt seed
dbt run --select demo_records --vars '{demo_scenario: initial}'
dbt test --select tag:dqm --vars '{demo_scenario: initial}'
dbt build --select package:dbt_dqm --vars '{demo_scenario: initial}'
dbt test --select assert_scenario_outcomes --vars '{demo_scenario: initial}'
```

The initial scenario runs three tracked tests with deliberately different shapes:

| Test | Granularity columns | Attribute columns | Initial failed rows |
| --- | ---: | ---: | ---: |
| Customer email | 1 | 2 | 3 |
| Order amount | 2 | 3 | 3 |
| Order line | 3 | 5 | 3 |

Case-only duplicate keys in the first two tests collapse at their configured grain. Repeat the
`run`/`test`/`build` sequence above with `demo_scenario: passed` (no rows, so every current issue
archives as a genuine pass) and then `demo_scenario: recurrence` (fresh occurrences for selected
identities), running the matching `assert_scenario_outcomes` step after each. That test replaces
what used to be a manual visual check: it asserts the exact expected active/archived occurrence
counts for whichever `demo_scenario` is currently loaded, so a reconciliation regression fails the
build instead of going unnoticed. It assumes the reset-then-`initial`-then-`passed`-then-`recurrence`
order shown above; running scenarios out of order or without the initial reset will make its
expectations (and the "Initial failed rows" table above) inapplicable. No company data or code is
included. See [`docs/warehouse-interfaces.md`](docs/warehouse-interfaces.md) for the package data
contracts.

## Postgres demo

The same walkthrough against Postgres lives in `integration_tests/demo_postgres` — same seed data,
same three tracked tests, same expected outcomes at each scenario, just a Postgres profile and no
BigQuery-specific schema-scoping macro:

```bash
cd integration_tests/demo_postgres
dbt deps
dbt run-operation reset_demo_dqm
dbt seed
dbt run --select demo_records --vars '{demo_scenario: initial}'
dbt test --select tag:dqm --vars '{demo_scenario: initial}'
dbt build --select package:dbt_dqm --vars '{demo_scenario: initial}'
dbt test --select assert_scenario_outcomes --vars '{demo_scenario: initial}'
```

Requires `create extension if not exists pgcrypto;` on the target database first (see
[Supported warehouses](#supported-warehouses)).

## Roadmap

The dbt package and the local review app are, and will stay, fully open source. A hosted/managed
offering is planned for the future — in the same spirit as how dbt Labs offers dbt Cloud alongside
the open-source dbt-core — but nothing about the package or app you're using today is gated behind
it. See [the current roadmap](docs/roadmap.md) for implemented gates and remaining work.

## License

Copyright 2026 Aliaksei Adamovich. Licensed under the Apache License, Version 2.0. See
[LICENSE.md](LICENSE.md).
