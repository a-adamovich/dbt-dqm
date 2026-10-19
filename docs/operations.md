# Install, operate, and upgrade dbt-dqm

The [README](../README.md) is the shortest path to a first run. This guide covers
compatibility, environment resolution, maintenance, and upgrades. The Python command and
dbt package are separate installations and must use matching 0.2 versions.

## Installation and compatibility

The stable release is `0.2.1`: [PyPI](https://pypi.org/project/dbt-dqm/0.2.1/) and
[Git tag `v0.2.1`](https://github.com/a-adamovich/dbt-dqm/releases/tag/v0.2.1).
The dbt package supports dbt Core 1.11–1.12 with dbt-bigquery 1.11–1.12 or dbt-postgres
1.11. There is no dbt-postgres 1.12; its 1.11 line supports dbt Core 1.12. Warehouse
support is BigQuery and Postgres 14/16. The Python app supports Python 3.11–3.14 and pins
dbt Core 1.12.5; its BigQuery extra uses dbt-bigquery 1.12.1 and its Postgres extra uses
dbt-postgres 1.11.x.

Until the [dbt Hub registration](https://github.com/dbt-labs/hubcap/pull/464) is live and
verified, install the dbt package from Git in the consumer project's `packages.yml`:

```yaml
packages:
  - git: "https://github.com/a-adamovich/dbt-dqm.git"
    revision: v0.2.1
```

Run `dbt deps`, then install the separate Python command in the environment where it
can find your project's dbt executable and warehouse adapter:

```bash
python -m pip install 'dbt-dqm[bigquery]==0.2.1'
# or: python -m pip install 'dbt-dqm[postgres]==0.2.1'
```

The extras select the adapter; `dbt-dqm[bigquery,postgres]` installs both. A plain
`dbt-dqm==0.2.1` install includes the command, app, and dbt Core, but no warehouse
adapter. Existing environments retain adapters already installed. In a source checkout,
`uv sync --extra bigquery` or `uv sync --extra postgres` installs only the needed
adapter; `uv sync --all-extras` installs both. The 0.2 app expects the matching 0.2 dbt
package; do not mix it with 0.1.

On Windows PowerShell, for example:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install 'dbt-dqm[bigquery]==0.2.1'
```

On Postgres, an administrator must create the `pgcrypto` extension once:
`create extension if not exists pgcrypto;`. The package does not create it because that
often requires elevated privileges. For BigQuery authentication, use the existing dbt
profile and credentials available to the command; the app uses the same profile.

The [release checklist](release-checklist.md) describes release gates, and the
[0.2.1 release record](release-0.2.1.md) records what was verified.

## Project, target, and artifact resolution

From a dbt project or subfolder, `dbt-dqm run`, `dbt-dqm app`, and
`dbt-dqm workspace` follow these defaults. Explicit flags take precedence:

- Project: `--project-dir`, then `DBT_PROJECT_DIR`, then dbt's current-directory
  and parent lookup.
- Profiles: `--profiles-dir`, then `DBT_PROFILES_DIR`, then `profiles.yml` in the
  launch directory, then the home directory's `.dbt` folder.
- Target: `--target`, then `DBT_TARGET`, then the rendered profile's `target`,
  or `default`.
- Artifact directory: `--target-path` on `run` or `app`, then
  `DBT_TARGET_PATH`, then the project's `target-path`, or `target`.
  Relative artifact and state paths resolve against the project directory.

`DBT_PROFILE`, profile `env_var()` values, and inherited authentication environment
variables remain available. Quote paths with spaces using your shell's conventions.
The app uses dbt beside its Python interpreter before searching `PATH`, and logs the
executable and version. Failed command details include exit codes and output streams.
If tracking relations are missing, run `dbt-dqm run` first.

The package and app resolve custom `generate_schema_name` and `generate_alias_name`
macros consistently. Pass the same naming `--vars` to `run`, `app`, and
`workspace`. Namespaces with different resolved prefixes have separate warehouse state
and local workspaces. Changing naming macros does not migrate or delete existing data.
`dbt_dqm_schema` can choose a tracking schema; with standard dbt schema naming, setting
it to `governance` produces `<target_dataset>_governance`.

The app can also read credentials supplied through `env_var()`. For BigQuery Workload
Identity Federation, provide a trusted external-account Application Default Credentials
configuration through `GOOGLE_APPLICATION_CREDENTIALS` to the command and app. Use a
dbt profile with `method: oauth`, for example:

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

Use your consumer project's profile name in place of `consumer`. The configuration and
token source must be accessible to both the command and app. Use the normal ADC environment
path rather than an explicit credentials-file override for WIF. dbt-dqm does not create
identity pools, providers, service accounts, IAM bindings, or credential files. Use an
environment you trust. The release's offline WIF smoke test
checks library behavior with simulated responses, not live IAM; live release QA uses
existing local credentials. [Scheduled BigQuery verification](scheduled-bigquery.md)
remains inactive until configured.

## Upgrading

Upgrade the Python command and dbt package together, run `dbt deps`, and restart the
review app. From `0.2.0a7` or `0.2.0a8`, run normally: no warehouse migration or full
refresh is required. From `0.2.0a4`–`a6`, history is preserved when resolved names
stay unchanged. If your alias macro prefixes package models, a7 and later also apply it
to state tables. An ordinary run refuses to initialize newly prefixed state tables while
recognizable legacy shared tables exist.

For a changed namespace, first apply or preserve pending edits in the old app and stop
old app processes. Then run `dbt-dqm run --full-refresh` using the same target and naming
variables you will use for the app. Review and confirm the exact resolved destinations;
add `--yes` for unattended use. This starts fresh isolated history and rebuilds only
that installation's resolved objects. Legacy shared state and other developer prefixes
remain untouched. Shared history is not copied or divided among developers; old local
drafts remain bound to their old workspace and must not be submitted to the new one.

Earlier versions require a full refresh. Preserve any annotations you need outside the
package first; automatic history migration is not provided. Update tests to the
[`meta.dbt_dqm` configuration](using-dbt-dqm.md#configure-tests), use native limits and
table failure storage, remove obsolete aliases, and set raw and event retention. A
confirmed full refresh resets known DQM tracking tables and package relations, assigns
a fresh installation identity, and runs DQM. It leaves consumer models, unrelated
tables, stored-failure relations, and the dataset in place. External dependent views
prevent a Postgres reset; deletion does not cascade to consumer objects. BigQuery
replacement copies preserve destination table access and verify table policies.

After an intentional reset, `dbt-dqm workspace purge` removes an obsolete local cache
and its pending edits before restarting the app. Annotation submissions from the
previous installation cannot be applied to the new one.

### Full-refresh confirmation

Validation happens before reset. The command prints the selected target and resolved
tracking relations and explains that history and annotations will be discarded. The
interactive confirmation defaults to **No**. Decline or EOF cancels with exit code `0`
and a cancellation message; noninteractive full refresh without `--yes` exits `2`
without resetting or running tests. Scheduled use must opt in explicitly:

```bash
dbt-dqm run --full-refresh --yes --target dev
```

An ordinary first installation does not need `--full-refresh`.

## Retention and maintenance

Set positive `dbt_dqm_retention_days` to prune processed raw executions, observations,
and old run-ledger rows. Set `dbt_dqm_event_retention_days` to prune issue event
history. With the default `dbt_dqm_event_payloads: full` and no event retention,
captured before/after values can be kept forever and may contain personal data.
`changed_columns` retains column names and hashes, which are not anonymization;
`none` omits the values. Choose a policy appropriate for your data.

Maintenance runs at the end of `dbt-dqm run`, or on demand with
`dbt run-operation cleanup_dqm_logs --vars "{dqm_enabled: true}"`. A SQL error inside
a maintenance step does not fail the dbt run; outcomes are recorded in
`dqm_maintenance_health` and the app's Health tab. See the
[warehouse contract](warehouse-interfaces.md#optional-maintenance) for the guarantees.

The experimental BigQuery option
`dbt-dqm run --vars "{dbt_dqm_maintenance_shortcuts: true}"` probes default automatic
cleanup steps and skips separate jobs only when no eligible work exists. Unknown
eligibility and overridden cleanup implementations run normally; raw-retention cutoffs
still advance. It is off by default because a live performance benefit has not been
established by credentialed benchmarks.

## Troubleshooting

- Confirm that the app, dbt package, target, and naming variables identify the same
  installation. A missing tracking relation usually means `dbt-dqm run` has not
  initialized it yet.
- Use `dbt-dqm run --verbose` for dbt listings, compiled SQL, and full phase output.
  The resolved artifact directory has `dqm_workflow_metrics.json`; dbt file logs
  remain available in quiet mode.
- If a collection reaches its limit, its positive evidence is captured but total
  failures are unknown; missing issues are not archived from that limited result.
  Increase the native limit or obtain a complete later execution.
- If deferring upstream models, use `--defer --state <state-artifacts>` and optionally
  `--favor-state`, keeping state input separate from `--target-path` output.
- `dbt-dqm workspace info` shows the local cache location and installation. Purge
  only when you intend to discard its snapshot and pending edits.

## Demo projects

[`integration_tests/demo_postgres`](../integration_tests/demo_postgres) and
[`integration_tests/demo_bigquery`](../integration_tests/demo_bigquery) contain the
same synthetic consumer: one seed, three tracked tests with different identity shapes,
and assertions for initial failures, Passed closures, and recurrences. No company data
is included. From either demo directory:

```bash
dbt deps
dbt run-operation reset_demo_dqm
dbt seed
dbt run --select demo_records --vars '{demo_scenario: initial}'
dbt-dqm run --vars '{demo_scenario: initial}'
dbt test --select assert_scenario_outcomes --vars '{demo_scenario: initial, dqm_enabled: true}'
```

Repeat the final three commands with `demo_scenario: passed`, then
`demo_scenario: recurrence`. Assertions assume that reset → initial → passed →
recurrence order. For Postgres, create `pgcrypto` first and start from
`profiles.yml.example`. For BigQuery, keep any service-account key outside the
repository; the demo's pre-created dataset needs BigQuery Job User on the project and
BigQuery Data Editor on that dataset.


## BigQuery dataset collation

Version 0.2.1 supports datasets whose default collation is `und:ci`. DQM tables explicitly use
`DEFAULT COLLATE ''` and tracking comparisons remain case-sensitive. The package does not change
your dataset default or your test's own comparison rules. Identity normalization still lowercases
and trims granularity values; original payload and owner casing is retained.

Install matching Python and dbt package versions and run `dbt deps`. Existing uncollated DQM
installations upgrade normally, without a schema migration or history reset.

If an earlier package created collated tracking columns, 0.2.1 refuses setup before tracking
writes. Changing a table's default does not repair existing columns, and BigQuery cannot change
their collation in place. For this affected installation only:

1. Export any history or annotations you need to retain, and stop its reviewer apps.
2. Upgrade both packages to 0.2.1 and run `dbt deps`.
3. Run `dbt-dqm run --full-refresh` with the same project, profile, target and naming `--vars`.
   Review the printed relation scope and confirm. This discards that installation's history and
   annotations; use `--yes` only after reviewing that scope for an unattended invocation.
4. Clear the installation's old reviewer workspaces and restart the apps. Old drafts are invalid.

The refresh preserves unrelated tables, dataset collation and existing table access. No automatic
reset or history-preserving conversion is performed. A separate tracking schema is another option
if you need to leave the old installation intact.
