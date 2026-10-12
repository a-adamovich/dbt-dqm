# dbt-dqm

dbt-dqm is an open-source dbt package for persistent, row-level tracking of dbt test failures,
plus a single-user local review app. Each failed row becomes an issue with a stable identity, a
lifecycle (Active → Archived when the test passes) and reviewer annotations that survive later
runs.

## Status and supported versions

| | |
| --- | --- |
| Release candidate | `0.2.0a1`, **alpha** (Git tag `v0.2.0a1` when published). Interfaces may change between alpha releases. dbt only accepts SemVer, so the package's `dbt_project.yml` spells the same version `0.2.0-a1`. |
| Upgrade from 0.1 | Needs a **fresh DQM schema**: set `dbt_dqm_schema` to a new schema. 0.1 tables are preserved and never reused or migrated. |
| dbt | dbt-core 1.11 and 1.12, with dbt-bigquery 1.11 or 1.12 and dbt-postgres 1.11 (there is no dbt-postgres 1.12; 1.11 supports dbt-core 1.12) |
| Warehouses | BigQuery; Postgres 14 and 16 (needs the `pgcrypto` extension) |
| Review app | Python 3.11, the same two warehouses |

The Python app pins dbt-core 1.12.5 and dbt-bigquery 1.12.1, with dbt-postgres 1.11.x;
the dbt package accepts the wider dbt range above. Python support is `>=3.11,<3.12`.
The [release checklist](docs/release-checklist.md) lists what must pass before the alpha is tagged.
Package contracts (relations, migrations, grants, recovery, retention, maintenance) are in
[warehouse interfaces](docs/warehouse-interfaces.md).

## Install the dbt package

Install the alpha through Git by adding it to your consumer project's `packages.yml`:

```yaml
packages:
  - git: "https://github.com/a-adamovich/dbt-dqm.git"
    revision: v0.2.0a1
```

The alpha is not listed on dbt Hub. Registration will be submitted after publication;
Hub's SemVer tag parser does not accept `v0.2.0a1`, so registration alone cannot make this
alpha installable through Hub. Keep using the Git revision above for this alpha.

The 0.2 app expects the matching 0.2 package; don't mix it with 0.1.

Then run `dbt deps`. The review app is a separate Python package (`dbt-dqm`); installing it doesn't
install the dbt package, and vice versa.

On Postgres, create the extension once (the package doesn't, because it usually needs elevated
privileges): `create extension if not exists pgcrypto;`

## Install the Python app and start reviewing

In a Python 3.11 environment, install the app separately:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install 'dbt-dqm==0.2.0a1'
```

Configure your dbt profile, add the Git package above, and set a fresh schema in your
consumer project's `dbt_project.yml` when upgrading from 0.1:

```yaml
vars:
  dbt_dqm_schema: dqm_alpha
```

After adding a tracked test as shown below, run these steps separately. A test that finds
failures can return a nonzero exit code; reconciliation must still run afterward:

```bash
dbt deps
dbt parse
dbt run --select package:dbt_dqm  # initialize tracking tables and public views
dbt test --select tag:dqm
dbt build --select package:dbt_dqm
dbt-dqm app --project-dir . --profiles-dir ~/.dbt --target dev
```

In the browser, Sync, edit an issue's notes or review verdict, then Apply. Health shows
setup, tests and maintenance status. The installation commands require the alpha to be
published; until then, they describe the release candidate's intended installation.

## BigQuery authentication through WIF

The app and dbt use Application Default Credentials (ADC) for an `oauth` profile. Supply a
trusted external-account configuration from your existing Workload Identity Federation
environment; it exchanges the external identity for short-lived service-account access
without a downloaded service-account key:

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/trusted-wif-adc.json
```

```yaml
consumer:
  target: dev
  outputs:
    dev:
      type: bigquery
      method: oauth
      project: your-project
      dataset: your_dataset
      location: US
      threads: 4
```

Use your consumer project's profile name in place of `consumer`. Keep the configuration
and its external token source available to both commands and the app. WIF configurations
belong to the environment owner; dbt-dqm does not create pools, providers, service accounts
or IAM bindings. Use the normal ADC environment path, not an explicit credentials-file
override, for WIF. Only use configurations from an environment you trust.

The offline WIF smoke test exercises ADC loading, token exchange, service-account
impersonation and refresh through the installed app/dbt authentication libraries, with
simulated responses. It does not verify live WIF or IAM. Live release QA uses existing
local service-account credentials; no new keys are created or downloaded. Optional
[scheduled BigQuery verification](docs/scheduled-bigquery.md) stays inactive until configured.

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

- `meta.dbt_dqm.granularity` is mandatory and is the only source of issue identity. Every failed
  row must return those columns; text identity comparisons are case-insensitive.
- Capture modes: `identity_only` (the default), `allowlist` (adds the named `context_columns`), and
  `full` (explicitly stores complete failed rows).

**Row owners and review metadata.** A failed row's `DQM_OWNER` column (or `owner_column`) sets its
owner, falling back to `poc_responsible` when blank or conflicting:

```sql
{{ config(tags=['dqm'], store_failures=true, severity='warn',
    meta={'dbt_dqm': {'granularity': ['campaign_id'], 'owner_column': 'DQM_OWNER',
      'poc_responsible': 'analytics', 'priority': 'high', 'criticality': 'customer reporting'}}) }}
select campaign_id, case when region='EU' then 'eu_team' else 'global_team' end as DQM_OWNER
from {{ ref('campaigns') }} where invalid_flag
```

Ownership is set only for new occurrences, and reviewer edits persist. Priority and tags are
visible and filterable; review verdicts (true/false positive) are separate from workflow status.

## Collect and reconcile

Run two separate dbt steps, so a failing test command never prevents reconciliation:

```bash
dbt test --select tag:dqm             # collection: an on-run-end hook captures tracked results
dbt build --select package:dbt_dqm    # reconciliation, public views and health views
```

Captures sharing stored-failure tables must be serialized: equal row counts do not prove
that the captured rows belong to the invocation. BigQuery supports one reconciliation per
dataset at a time; overlapping reconciliations are detected, not queued.

`dbt run --select dbt_dqm.dqm_reconcile` runs reconciliation alone. The package publishes
`dqm_current_issues`, `dqm_all_issues`, `dqm_issue_timeline`, the health views and the tracking
tables in the DQM schema. `dbt_dqm_test_tag` and `dbt_dqm_schema` override the defaults; with
dbt's standard schema naming, `dbt_dqm_schema: governance` produces `<target_dataset>_governance`.

## Retention and maintenance

- `dbt_dqm_retention_days` (a positive integer) prunes raw executions and observations that
  reconciliation has already processed, and old run-ledger rows.
- `dbt_dqm_event_retention_days` prunes the `dqm_issue_events` history.

These run as optional maintenance after invocations that captured tracked tests or reconciled, or
on demand with `dbt run-operation cleanup_dqm_logs`. A SQL error inside a maintenance step never
fails the dbt run; outcomes are logged and shown in `dqm_maintenance_health` and the app's Health
tab. The exact guarantee and its limits are in
[warehouse interfaces](docs/warehouse-interfaces.md#optional-maintenance).

> **Event values outlive raw logs by default.** With `dbt_dqm_event_payloads: full` (the default)
> and no `dbt_dqm_event_retention_days`, captured before/after values are kept forever. Under
> `allowlist` or `full` capture they can contain personal data. Set event retention, or choose
> `changed_columns` (column names plus hashes, which are not anonymization) or `none`.

## Local review app

```bash
dbt-dqm app \
  --project-dir /path/to/consumer-project \
  --profiles-dir ~/.dbt \
  --target demo
```

- It uses the selected dbt profile (validated with `dbt debug`), including `env_var()`
  authentication and custom `generate_schema_name` behavior, and synchronizes at the start of each
  browser session.
- It caches a project-and-target-specific snapshot plus pending edits in an owner-only SQLite
  workspace under the user-data directory. Credentials are never copied into it.
  `dbt-dqm workspace info` and `dbt-dqm workspace purge` (with `--project-dir` and `--target`)
  inspect or delete it.
- Edits use compare-and-set versions: a concurrent change produces an explicit conflict rather than
  a silent overwrite. On Postgres, a write blocked by a running reconciliation gives up after
  `--lock-timeout` seconds (default 5) and reports the warehouse as busy.
- Each sync is verified before it replaces the cache; an inconsistent read keeps the cache and
  pending edits.
- **Cache limits:** at most 50,000 issues and 128 MiB of serialized issue data
  (`--max-cache-issues` and `--max-cache-mib` can only lower them). The cache holds Active issues
  plus 90 days of Archived history; `--archive-cache-days 0` keeps Active issues only. An
  oversized sync keeps the existing cache and pending edits. These limits bound serialized data,
  not process memory; see [scale](docs/scale.md).
- **Backlogs above the cache limits (`--browse`, default `auto`):** the app syncs a local
  snapshot while the review population fits the limits, and above them reads issues from the
  warehouse page by page instead. Filters, search, counts and the sort then run in the
  warehouse; only recently loaded pages (up to 2,000 issues or 32 MiB) and issues with pending
  edits are kept locally, and edits keep the same compare-and-set versions. When the warehouse
  can't be read, the cached pages are shown offline. `--browse snapshot` or `--browse warehouse`
  forces a mode.
- **Warehouse cost in that mode:** every page, count and search is a query. On BigQuery the app
  reads 2,000 issues at a time (about 1 GB billed at 1M occurrences, serving 80 pages of 25), a
  header summary costs about 0.16 GB, and a search is refused before it runs if a dry run
  estimates more than `--max-search-gib` (default 1). Postgres needs migration 0007's index for
  fast pages. Measured figures: [scale](docs/scale.md#warehouse-browsing-2026-10-11).

## Demo projects

`integration_tests/demo_postgres` and `integration_tests/demo_bigquery` contain the same synthetic
consumer: one seed, three tracked tests of different shapes, and an assertion of the expected
outcome for each scenario. No company data is included.

```bash
cd integration_tests/demo_postgres   # or integration_tests/demo_bigquery
dbt deps
dbt run-operation reset_demo_dqm
dbt seed
dbt run --select demo_records --vars '{demo_scenario: initial}'
dbt test --select tag:dqm --vars '{demo_scenario: initial}'
dbt build --select package:dbt_dqm --vars '{demo_scenario: initial}'
dbt test --select assert_scenario_outcomes --vars '{demo_scenario: initial}'
```

Repeat the last four commands with `demo_scenario: passed` (every issue archives as a genuine
pass), then `demo_scenario: recurrence` (fresh occurrences for selected identities). The scenario
assertion checks the exact active and archived counts, and assumes this reset → initial → passed →
recurrence order.

| Test | Granularity columns | Attribute columns | Initial failed rows |
| --- | ---: | ---: | ---: |
| Customer email | 1 | 2 | 3 |
| Order amount | 2 | 3 | 3 |
| Order line | 3 | 5 | 3 |

Adapter notes:
- **Postgres:** create `pgcrypto` first; start from the demo's `profiles.yml.example`.
- **BigQuery:** keep the service-account key outside the repository (for example
  `~/.credentials/dbt-dqm/service-account.json`). The demo's schema macro stores test failures in
  the same pre-created dataset, so its account needs only BigQuery Job User on the project and
  BigQuery Data Editor on that dataset.

## Roadmap

The package and the local app are, and will stay, Apache-2.0 open source. A hosted offering may
follow, but nothing in the package or app is gated behind it. See the [roadmap](docs/roadmap.md).

## License

Copyright 2026 Aliaksei Adamovich. Licensed under the Apache License, Version 2.0. See
[LICENSE.md](LICENSE.md).
