# CLI reference

`dbt-dqm run` executes tracked tests, captures evidence, reconciles issues, publishes views, and runs maintenance. `dbt-dqm app` starts the local review app. `dbt-dqm workspace info` reports the local cache path, whether it exists, and its size, and `dbt-dqm workspace purge` deletes its snapshot and pending edits. Use `--help` at each command level. The CLI rejects unknown options.

## Common options and resolution

| Option | Applies to | Resolution |
| --- | --- | --- |
| `--project-dir` | All | Flag, `DBT_PROJECT_DIR`, then the dbt project in the working directory or a parent. |
| `--profiles-dir` | All | Flag, `DBT_PROFILES_DIR`, then the working directory if it contains `profiles.yml`, then `~/.dbt`. |
| `--target` | All | Flag, `DBT_TARGET`, then the rendered profile target or `default`. |
| `--dbt-executable` | All | Flag, `DBT_DQM_DBT_EXECUTABLE`, the dbt executable beside app Python, then `PATH`. |
| `--vars` | All | A dbt YAML mapping. Defaults to `{}`. Pass identical naming variables to all commands that use the same installation. |
| `--target-path` | Run, app | Flag, `DBT_TARGET_PATH`, then project `target-path` or `target`. Relative artifact and state paths resolve against the project directory. |

`DBT_PROFILE`, profile `env_var()` values, and inherited authentication environment variables remain available. Quote paths containing spaces. Pass variables in PowerShell with syntax such as `--vars "dbt_dqm_default_test_limit: 250"`.

## Run options

| Option | Behavior |
| --- | --- |
| `--select`, `--exclude` | dbt selector expressions. The workflow runs only selected tests carrying `meta.dbt_dqm`. |
| `--threads` | Override the profile's thread count. |
| `--defer`, `--no-defer` | Explicitly enable or disable deferral. Otherwise inherit `DBT_DEFER`. |
| `--favor-state`, `--no-favor-state` | Explicitly prefer state for upstream refs or use normal deferral. Otherwise inherit `DBT_FAVOR_STATE`. |
| `--state` | Directory of comparison and deferral artifacts. Otherwise inherit `DBT_STATE`. The runner can also inherit `DBT_DEFER_STATE`. |
| `--summary` | Query and print the persisted owner summary. |
| `--verbose` | Print full dbt console output and timings. Normal output includes phase progress, warnings, and one line per test. Both modes retain dbt file logs. |
| `--full-refresh`, `--yes` | Reset the resolved installation's tracking history and annotations. Use `--yes` to confirm in a noninteractive shell. |

Deferral affects consumer test compilation and execution. Setup, capture, reconciliation, and maintenance stay in the selected target. Keep state input separate from artifact output so parsing cannot overwrite the state manifest.

```bash
dbt-dqm run --select tag:customer --exclude tag:experimental --threads 4
dbt-dqm run --defer --favor-state --state prod-artifacts --target-path dev-artifacts
```

### Exit codes and full refresh

| Code | `run` result |
| --- | --- |
| 0 | Success, including warning-severity test results, a cancelled refresh, or no matching tracked tests. |
| 1 | Severity-error test failures after evidence capture and reconciliation. |
| 2 | Invalid options or configuration, execution or capture errors, or a test error or skip. A noninteractive full refresh without `--yes` also exits 2. |

The command validates selected tests before resetting. It prints the selected target and resolved tracking relations and prompts for confirmation. The interactive default is **No**. Declining or reaching EOF cancels with exit `0`. An ordinary first installation can run without a full refresh.

```bash
dbt-dqm run --full-refresh --yes --target dev
```

### Owner summary and metrics

```text
DQM owner summary:
Owner       OPENED (NEW)  RECURRING  ARCHIVED  NEW (status)  ACTIVE
Total                  1          0         0             1       1
pilot_team             1          0         0             1       1
Unassigned             0          0         0             0       0
OPENED (NEW), RECURRING and ARCHIVED are changes applied by this reconciliation. NEW (status) is the active review workload with status NEW; ACTIVE includes it.
```

The first three counts include previously pending evidence applied by this reconciliation. `ARCHIVED` counts Passed closures. Structural closures appear on a separate line when present. `NEW (status)` and `ACTIVE` describe the current workload across the installation. Owners are sorted by NEW workload, then by name, using saved assignments. Unassigned is always included. Summary read failures warn without changing the workflow exit.

The artifact directory contains `dqm_workflow_metrics.json`, with available timings, SQL counts, BigQuery job IDs, collection limits, and summary results. Available metrics are written even after an execution failure. Use `--verbose` and dbt logs to diagnose failing phases. If a collection reaches its limit, increase the limit or run a later complete collection before relying on absence to resolve an issue.

## App options

| Option | Default and bounds |
| --- | --- |
| `--port` | `8501`. The app listens on `127.0.0.1`. |
| `--browse` | `auto`. Use `snapshot` or `warehouse` to force a mode. |
| `--max-cache-issues` | 50,000 issues, configurable to a positive integer at or below that ceiling. |
| `--max-cache-mib` | 128 MiB of serialized issue data, configurable to a positive integer at or below that ceiling. |
| `--max-search-gib` | 1 GiB per BigQuery search or warehouse export. Accepts values greater than 0 and up to 1024. |
| `--archive-cache-days` | 90 days. Use `0` for active issues only in the snapshot. |
| `--lock-timeout` | 5 seconds for Postgres annotation writes. Accepts positive integers. |

The app exits `1` for invalid app option values and `130` on a keyboard interrupt. Once started, it reads existing tracking relations. If they are missing, verify the project, target, and naming variables and run `dbt-dqm run` first. See [review app workflows](review-app.md) for browsing and edits.
