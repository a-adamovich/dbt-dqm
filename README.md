# dbt-dqm

dbt-dqm is an open-source dbt package for persistent, row-level tracking of dbt test failures,
plus a single-user local review app. Each failed row becomes an issue with a stable identity, a
lifecycle (Active → Archived when the test passes) and reviewer annotations that survive later
runs.

## Status and supported versions

| | |
| --- | --- |
| Latest release | `0.2.0a7`, **alpha**: [PyPI](https://pypi.org/project/dbt-dqm/0.2.0a7/), Git tag [`v0.2.0a7`](https://github.com/a-adamovich/dbt-dqm/releases/tag/v0.2.0a7). Interfaces may change between alpha releases. dbt only accepts SemVer, so the package's `dbt_project.yml` spells the same version `0.2.0-a7`. |
| Upgrade | From a4–a6: update both packages. Unchanged resolved names retain history; corrected prefixed namespaces require a confirmed fresh start. See [Migrating existing tests](#migrating-existing-tests) before upgrading. Earlier versions require `dbt-dqm run --full-refresh`. |
| dbt | dbt-core 1.11 and 1.12, with dbt-bigquery 1.11 or 1.12 and dbt-postgres 1.11 (there is no dbt-postgres 1.12; 1.11 supports dbt-core 1.12) |
| Warehouses | BigQuery; Postgres 14 and 16 (needs the `pgcrypto` extension) |
| Review app | Python 3.11–3.14, the same two warehouses |

The Python app pins dbt-core 1.12.5; its optional extras use dbt-bigquery 1.12.1 and dbt-postgres 1.11.x;
the dbt package accepts the wider dbt range above. Python support is `>=3.11,<3.15`.
The [release checklist](docs/release-checklist.md) lists what must pass before a release is tagged;
the [0.2.0a7 evidence](docs/release-0.2.0a7.md) records this alpha's results.
Package contracts (relations, migrations, grants, recovery, retention, maintenance) are in
[warehouse interfaces](docs/warehouse-interfaces.md).

## Install the dbt package

Install the alpha through Git by adding it to your consumer project's `packages.yml`:

```yaml
packages:
  - git: "https://github.com/a-adamovich/dbt-dqm.git"
    revision: v0.2.0a7
```

The alpha is not listed on dbt Hub: Hub's SemVer tag parser does not accept `v0.2.0a7`, so
install this alpha through the Git revision above. Hub registration is planned for 0.2.0.

The 0.2 app expects the matching 0.2 package; don't mix it with 0.1.

Then run `dbt deps`. The review app is a separate Python package (`dbt-dqm`); installing it doesn't
install the dbt package, and vice versa.

On Postgres, create the extension once (the package doesn't, because it usually needs elevated
privileges): `create extension if not exists pgcrypto;`

## Install the Python app and start reviewing

In a Python 3.14 environment (recommended; 3.11–3.14 supported), install the app separately:

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install 'dbt-dqm[bigquery]==0.2.0a7'
```

Warehouse adapters are optional. In a checkout, install
only the adapter you need with `uv sync --extra bigquery` or `uv sync --extra postgres`;
use `uv sync --all-extras` for both. Select the extra in your installed environment:

```bash
python -m pip install 'dbt-dqm[bigquery]==0.2.0a7'          # BigQuery
python -m pip install 'dbt-dqm[postgres]==0.2.0a7'          # Postgres
python -m pip install 'dbt-dqm[bigquery,postgres]==0.2.0a7' # Both
```

A plain install includes the app and dbt-core but no warehouse adapters.
Shared UI dependencies such as PyArrow remain installed. Existing environments retain adapters
already installed. Earlier 0.2.0a2 installs included both adapters.

On Windows PowerShell, create and activate the environment with:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install 'dbt-dqm[bigquery]==0.2.0a7'
```

Activation is optional: `uv run --extra bigquery dbt-dqm app` uses the checkout environment.
For an installed environment, run its `dbt-dqm` command directly.

Configure your dbt profile, add the Git package above, and set a fresh schema in your
consumer project's `dbt_project.yml` when upgrading from 0.1:

```yaml
vars:
  dbt_dqm_schema: dqm_alpha
```

After adding a DQM test as shown below, run it separately from your regular model workflow:

```bash
dbt deps
dbt-dqm run
dbt-dqm app
```

The dedicated command initializes tracking relations, captures results, and reconciles even when
quality tests fail. Project, profiles and target flags remain optional for both commands.

In the browser, Sync, edit an issue's notes or review verdict, then Apply. Health shows
setup, tests and maintenance status.

## Configuration defaults and troubleshooting

From your dbt project or a subfolder, launch with `dbt-dqm app`; `dbt-dqm workspace info`
and `dbt-dqm workspace purge` use the same defaults. Explicit flags take precedence:

- Project: `--project-dir`, then `DBT_PROJECT_DIR`, then dbt's current-directory/parent lookup.
- Profiles: `--profiles-dir`, then `DBT_PROFILES_DIR`, then `profiles.yml` in the launch directory,
  then your home directory's `.dbt` folder.
- Target: `--target`, then `DBT_TARGET`, then the rendered profile's `target`, or `default`.
- Artifact directory: `--target-path` on `run` or `app`, then `DBT_TARGET_PATH`, then the project's
  `target-path`, or `target`. Relative artifact and state paths resolve against the project directory.

`DBT_PROFILE`, `DBT_TARGET_PATH`, profile `env_var()` values, and
`GOOGLE_APPLICATION_CREDENTIALS` are inherited. Project, profiles and target defaults stay with
dbt; the worker passes its resolved artifact directory consistently to every phase.
Paths use the native operating system's conventions; quote paths containing spaces.
The app uses dbt beside its own Python interpreter before searching PATH, and logs that
executable and its version. Technical details include failed commands, exit codes and both output
streams. If tracking relations are missing, run `dbt-dqm run` first.
This package command works at the normal multithreaded setting; its setup models are ordered.

Custom `generate_schema_name` and `generate_alias_name` macros are supported for package models.
The app and SQL helpers use the parsed model relations. Macro-created internal tracking tables,
such as `dqm_issue_occurrences`, keep their fixed identifiers in the reconciliation model's schema.
Changing naming macros does not migrate or delete existing warehouse data.

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

## Migrating existing tests

DQM is disabled by default and runs as a separate workflow. Install the matching warehouse extra,
update the Git package revision above, and run `dbt deps`. For a4–a6 installations whose resolved relation names are unchanged, run `dbt-dqm run` without a
full refresh; history and annotations are preserved. **If your alias macro prefixes package models,
a7 now applies it to state tables too.** An ordinary run refuses to silently initialize the new
namespace while legacy shared tables exist. Apply or preserve pending edits in the old app before
upgrading both the Python app and dbt Git package, stop old app processes, then run
`dbt-dqm run --full-refresh` with the same target and naming variables. Review and confirm the exact
resolved destinations (`--yes` for unattended use). This starts fresh isolated history and rebuilds
that namespace's incremental models; old shared tables and other developer prefixes remain untouched.
Shared history is not copied or automatically divided among developers. Old local drafts remain in
their old workspace and must not be submitted to the fresh installation.

The reset procedure below also applies to older versions or an intentional history reset:

1. Stop reviewer apps and preserve any annotations you need outside the package. No history migration is provided.
2. Mark each DQM test with a `meta.dbt_dqm` object. Tags are optional business categories; the legacy
   `dqm` tag remains stored but is excluded from app displays and tag filters.
3. Set `enabled=var('dqm_enabled', false)`, `store_failures=true`, and
   `meta.dbt_dqm.granularity` to the output columns identifying the failed record.
4. Use native `limit`, normally `limit=var('dbt_dqm_default_test_limit', 100)`.
   Set `dbt_dqm_default_test_limit` in `dbt_project.yml` to change that default, or give an individual
   test its own positive integer limit. A missing native limit warns and uses the project default.
   Keep `fail_calc=count(*)` and table failure storage.
5. Set ownership using a nonblank output column `dqm_owner`, `meta.dbt_dqm.owner`, or `meta.owner`,
   in that order. Blank values fall through. Conflicting row owners use the metadata fallback and
   produce a Health diagnostic. Ownership initializes new issues; later reviewer edits persist.
   Both metadata keys accept a string, a list of strings, or null, including inherited folder-level
   `+meta`. List entries are trimmed, blanks discarded, and remaining entries joined with `, ` in
   list order. An empty list falls through. The joined owner remains one text field in the app;
   original metadata is retained in execution history. Other types fail validation before a reset.
6. All output keys and attributes are captured. Remove `capture_mode`, `dbt_dqm_capture_mode`, and
   `context_columns`; they warn and are ignored. Remove `poc_responsible`, `owner_column`,
   `dqm_granularity`, and `dbt_dqm_test_tag`; these obsolete aliases are unsupported.
7. Configure retention for captured observations and event payloads. The dedicated workflow warns
   when either full-value history has no corresponding retention setting.
8. Run `dbt-dqm run --full-refresh`. This explicitly resets known DQM tracking tables and
   manifest-resolved package relations, assigns a fresh installation identity, then runs DQM.
   It preserves consumer models, stored-failure relations, unrelated tables, and the dataset.
   On PostgreSQL an external dependent view prevents reset; deletion never cascades to consumer objects.
   On BigQuery empty replacement copies preserve destination table access; reset verifies table policies.
9. Clear each old local workspace with `dbt-dqm workspace purge`, then restart reviewer apps.
   Old annotation submissions are bound to the previous installation and cannot be applied after reset.

Example singular test (the same configuration belongs under `config:` for a generic YAML test):

```sql
{{ config(
    enabled=var('dqm_enabled', false),
    store_failures=true,
    limit=var('dbt_dqm_default_test_limit', 100),
    tags=['customer', 'contact'],
    meta={'owner': 'analytics', 'dbt_dqm': {
      'granularity': ['customer_id'], 'owner': 'customer_support', 'priority': 'high'
    }}
) }}
select customer_id, reason, assigned_team as dqm_owner
from {{ ref('customers') }} where is_invalid
```

```yaml
# dbt_project.yml
vars:
  dbt_dqm_default_test_limit: 100
  dbt_dqm_retention_days: 30
  dbt_dqm_event_retention_days: 90
```

## Run the dedicated workflow

From your dbt project, after your regular models have run:

```bash
dbt-dqm run                         # enable DQM, validate, test, capture, reconcile, maintain
dbt-dqm run --select tag:customer --exclude tag:experimental --threads 4
dbt-dqm run --vars '{dbt_dqm_default_test_limit: 250}'
dbt-dqm run --full-refresh          # explicit reset; tracking history is discarded
dbt-dqm run --defer --favor-state --state prod-artifacts --target-path dev-artifacts
dbt-dqm app --target-path dev-artifacts
dbt-dqm run --verbose               # full dbt output, including listings, SQL and phase timings
```

PowerShell uses the same commands. To avoid shell differences for YAML variables:

```powershell
dbt-dqm run --vars "dbt_dqm_default_test_limit: 250"
dbt-dqm run --full-refresh
dbt-dqm run --defer --favor-state --state "prod artifacts" --target-path "dev artifacts"
dbt-dqm app --target-path "dev artifacts"
dbt-dqm workspace purge
```

Project, profiles and target flags are optional and follow the dbt/environment defaults described
above. The worker uses dbt's programmatic runner in this Python environment, preserving the launch
working directory and inherited credentials. Large selections and result payloads stay out of the
Windows subprocess command line. Only metadata-marked tests matching the selection run; invalid
selected configurations stop before initialization or reset. App startup only parses metadata and reads
warehouse state; it does not run tests, capture results or execute package models.

Normal output contains phase progress, validation messages, warnings, test failures, and a final
test/capture/time summary. `--verbose` displays full dbt console output. dbt's file logging remains
available in both modes. Phase timings, SQL counts and BigQuery job IDs are saved in
`dqm_workflow_metrics.json` in the resolved artifact directory, including available metrics after
an execution failure. Full-refresh timing is separate from initialization; maintenance step actions
and BigQuery durations are included.

`--defer`, `--favor-state`, and `--state` override `DBT_DEFER`, `DBT_FAVOR_STATE`, and `DBT_STATE`.
Without explicit flags the installed dbt runner inherits these environment settings (and
`DBT_DEFER_STATE` for separate deferral artifacts). `--no-defer` and `--no-favor-state` explicitly
override enabled environment settings. State selection uses the supplied manifest; deferral applies
to consumer test compilation and execution. DQM setup, capture, reconciliation, and maintenance
always use the selected target. Input state and output artifacts must be separate directories;
the command rejects identical resolved paths before parsing can overwrite a state manifest.

Full refresh prints the selected target and resolved tracking relations, then asks for confirmation
(default **No**) after test validation. History and annotations are discarded only after confirmation.
For scheduled/noninteractive commands, explicitly add `--yes`:

```bash
dbt-dqm run --full-refresh --yes --target dev
```

The same command works in PowerShell. A declined interactive reset exits `0` with cancelled metrics;
a noninteractive reset without `--yes` exits `2`. Upgrading from a4 or a5 requires no full refresh.

Test failures still produce captured evidence and reconciliation. Exit codes are `0` for success,
`1` for failed tests, and `2` for configuration or execution errors. A dbt `warn` result retains dbt's
successful exit behavior. Automatic capture and retention `on-run-end` hooks have been removed.
Ordinary dbt commands leave DQM disabled unless you explicitly enable it.

Limits apply to raw failed rows before deduplication. Samples are ordered by granularity identity
and full payload before the native limit. Reaching the exact limit is ambiguous: the full failure
count is unknown. Such executions have `collection_status=limit_reached`, plus `failure_limit`
and `captured_issue_count`. They reconcile observed positive evidence but never close omitted
issues through absence. A later complete execution can resolve them. Health shows limited collections.

Serialize DQM workflows sharing stored-failure tables: equal counts do not prove two invocations
read the same rows. BigQuery supports one reconciliation per resolved DQM installation at a time. Per-test capture
commits preserve earlier successful captures if a later test fails during collection.

`dbt_dqm_schema` optionally chooses the tracking schema. With standard dbt schema naming,
`dbt_dqm_schema: governance` produces `<target_dataset>_governance`. Package model relations respect
custom schema and alias macros; macro-owned tracking identifiers remain fixed under the
reconciliation node's database and schema. Changing naming macros does not migrate or delete old tables.

## Retention and maintenance

- `dbt_dqm_retention_days` (a positive integer) prunes raw executions and observations that
  reconciliation has already processed, and old run-ledger rows.
- `dbt_dqm_event_retention_days` prunes the `dqm_issue_events` history.

These run as the last phase of `dbt-dqm run`, or on demand with
`dbt run-operation cleanup_dqm_logs --vars "{dqm_enabled: true}"`. A SQL error inside a maintenance step never
fails the dbt run; outcomes are logged and shown in `dqm_maintenance_health` and the app's Health
tab. The exact guarantee and its limits are in
[warehouse interfaces](docs/warehouse-interfaces.md#optional-maintenance).

An experimental BigQuery optimization can be enabled with
`dbt-dqm run --vars "{dbt_dqm_maintenance_shortcuts: true}"`. It probes default automatic cleanup
steps and skips separate jobs only when no eligible work exists, recording an attributed success
with a fixed "no eligible work" description. Unknown eligibility and custom cleanup implementations
execute normally. Raw retention still advances its cutoff and generation fence, while proven-empty
deletes are omitted. Manual cleanup executes its steps. This optimization is **off by default** until
credentialed benchmarks establish a benefit; SQL-rendering tests do not establish a live speedup.

> **Event values outlive raw logs by default.** With `dbt_dqm_event_payloads: full` (the default)
> and no `dbt_dqm_event_retention_days`, captured before/after values are kept forever. Under
> full capture they can contain personal data. Set event retention, or choose
> `changed_columns` (column names plus hashes, which are not anonymization) or `none`.

### Owner workload summary

`dbt-dqm run --summary` keeps the normal completion line and adds current **NEW** and **ACTIVE**
counts grouped by the saved owner shown in the app, including reviewer edits. ACTIVE is every
unarchived issue in this installation; NEW is the subset with workflow status NEW. ACTIVE includes
NEW and can include issues retained when a collection hits its limit. These are current workload
counts, not changes introduced by this invocation. Total appears first, owners sort by NEW descending,
and Unassigned is always included. Comma-separated owners remain one label. Without `--summary`,
no extra reporting query runs. A reporting failure warns without changing the workflow exit code.

Package state tables, views, incremental models and staging objects follow the project's
`generate_alias_name`; custom schemas follow the package's resolved model schema. Set the same
variables when opening the app or inspecting a workspace. Name resolution works without a live
warehouse connection, so cached offline browsing and retry remain available:

```bash
dbt-dqm run --vars '{table_alias_prefix: alice}' --summary
dbt-dqm app --vars '{table_alias_prefix: alice}'
dbt-dqm workspace info --vars '{table_alias_prefix: alice}'
```

PowerShell accepts the same single-quoted YAML arguments. Normal console output contains one line
per selected test with its status and reported row count; `--verbose` retains full dbt output.

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
  inspect or delete it. Resolved installations have separate local workspaces; use the same
  `--vars` naming overrides as `run`. Switching namespace in an open session requires restarting it.
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

### Bulk editing and exports

Select individual issues or **Select current page**, move between pages, and choose bulk owner,
workflow status or review verdict. Each field defaults to **No change**; owner supports unassignment.
**Stage changes** creates local pending edits for the selected issues. **Apply and sync** submits them
through the same conflict detection, audit and retry mechanism as individual edits. Chosen fields
replace existing drafts for those fields; notes, tickets and other drafts are kept. Selection is
limited to 2,000 issues or 32 MiB and clears after staging, filter changes, Sync or successful Apply.
There is no automatic selection of every issue belonging to a test.

Expand **Export saved issues** above the issue cards. **Prepare CSV** and **Prepare Excel** export all filtered results across pages, using saved values;
pending edits are excluded. Archived issues are included only when the current lifecycle filter and
archive window include them. Disable **Pending edits only** before exporting.

Export columns start with test name, owner and captured data (Excel `record.*`, CSV
`record_values_json`), followed by lifecycle/workflow status, verdict, test status, first seen,
last seen, annotation updated, call to action, ticket URL, notes, then remaining metadata.
Excel puts configured granularity fields first within captured data, followed by captured JSON
field order; heterogeneous fields append in first-seen order. Timestamp headers explicitly label
UTC, included offsets or an unspecified timezone. Values retain their original precision and offsets;
there is no implicit Eastern-time conversion, and captured JSON timestamps are untouched.

- CSV is UTF-8 with BOM and standard quoting. It preserves `record_values_json` and other JSON
  columns. Formula-like text is prefixed with an apostrophe so opening it in a spreadsheet does not
  execute formulas; captured JSON remains intact.
- Excel has one sheet per test represented in the filtered results. It expands top-level
  `record_values_json` fields into `record.<field>` columns alongside review metadata. Nested objects,
  arrays and other JSON remain text; captured IDs and numeric-looking strings retain their exact
  text. Long or duplicate test names get safe, unique sheet names; full test names and IDs remain in
  the rows. Captured numeric tokens retain their full precision as text; nested JSON retains its
  complete original content. Headers are frozen and filterable. Values exceeding Excel's limits require CSV instead.

Exports are limited to 50,000 issues, 128 MiB of serialized source data and 128 MiB per file,
independently of browsing cache settings. Limit failures offer no partial file. Warehouse exports
use a single filtered data query with installation/generation checks; on BigQuery the whole query,
even without search, respects `--max-search-gib`. Narrow filters to reduce size or cost. Prepared
files are temporary and are discarded after filter changes, Sync or Apply.

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
dbt-dqm run --vars '{demo_scenario: initial}'
dbt test --select assert_scenario_outcomes --vars '{demo_scenario: initial, dqm_enabled: true}'
```

Repeat the last three commands with `demo_scenario: passed` (every issue archives as a genuine
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
