# dbt-dqm

dbt-dqm is a source-available dbt package for persistent, row-level test issue tracking plus a
single-user local review app. The v1 adapter is BigQuery and the supported dbt line is 1.11.x.

## Configure a tracked test

```sql
{{
  config(
    tags=['dqm'],
    store_failures=true,
    meta={'dbt_dqm': {'granularity': ['customer_id']}}
  )
}}

select customer_id, reason from {{ ref('customers') }} where is_invalid
```

Every failed-row query must return the configured granularity columns. Text identity comparisons
are case-insensitive; original key and attribute values remain visible to reviewers. There is no
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

## Local app

```bash
dbt-dqm app \
  --project-dir /path/to/consumer-project \
  --profiles-dir ~/.dbt \
  --target demo
```

The app uses the selected dbt BigQuery profile, validates it with `dbt debug`, and automatically
synchronizes at the start of each browser session. It stores a project-and-target-specific snapshot
plus field-level pending changes in a SQLite workspace under the operating system's user-data
directory. Pending edits are overlaid after every refresh, and credentials are never copied into the
workspace.

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

## Roadmap

The dbt package and the local review app are, and will stay, fully open source. A hosted/managed
offering is planned for the future — in the same spirit as how dbt Labs offers dbt Cloud alongside
the open-source dbt-core — but nothing about the package or app you're using today is gated behind
it.

## License

Copyright 2026 Aliaksei Adamovich. Licensed under the Apache License, Version 2.0. See
[LICENSE.md](LICENSE.md).
