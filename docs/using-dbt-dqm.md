# Configure tests and run dbt-dqm

dbt-dqm tracks dbt data tests that you opt into with `meta.dbt_dqm`. A dedicated
`dbt-dqm run` enables them, validates their output, runs them, captures failed rows, and
reconciles the issue history. Ordinary dbt commands leave tests configured with
`enabled=var('dqm_enabled', false)` disabled.

## Configure tests

Use a singular SQL test, or put the same configuration under `config:` for a generic YAML test.
The query should return one row per failure. This example captures both the identity and the
descriptive values of a failed customer:

```sql
{{ config(
    enabled=var('dqm_enabled', false),
    store_failures=true,
    limit=var('dbt_dqm_default_test_limit', 100),
    meta={'dbt_dqm': {
        'granularity': ['customer_id'],
        'owner': 'customer_data',
        'priority': 'high',
        'call_to_action': 'Correct the customer email address'
    }}
) }}

select customer_id, email, reason
from {{ ref('customers') }}
where email is null
```

- `meta.dbt_dqm.granularity` is required: list the output columns that identify one issue.
  A change in another column updates the same issue; a change in the granularity definition
  closes old identities structurally and starts new ones. Duplicate failed rows at the same
  grain form one issue observation. Choose columns that distinguish the records people review.
- Set `store_failures=true`, use table failure storage, and leave dbt's
  `fail_calc=count(*)`. Use native `limit` with a positive integer. The project default can
  be set as `dbt_dqm_default_test_limit` in `dbt_project.yml`; a missing native limit warns
  and falls back to 100 unless you change that default.
- All returned columns and values are captured. Tags are optional business categories; the
  `dqm` tag is not needed to opt in. Legacy `capture_mode`, `dbt_dqm_capture_mode`, and
  `context_columns` warn and are ignored. Obsolete `poc_responsible`, `owner_column`,
  `dqm_granularity`, and `dbt_dqm_test_tag` aliases are unsupported.
- A nonblank failure-row `dqm_owner` takes precedence over `meta.dbt_dqm.owner`, then
  `meta.owner`. Metadata owners can be strings, lists of strings, or null; list entries are
  trimmed and joined with `, `. Blank values fall through. Conflicting row owners use the
  metadata fallback and produce a Health diagnostic. The resulting owner initializes an issue;
  a reviewer's later owner edit is preserved.

Validation runs before any full-refresh reset. An invalid selected test stops the command
before tracking relations are initialized or history is discarded.

## Run and interpret results

Run upstream models as you normally would, then use the dedicated command from your dbt
project. Only selected tests carrying `meta.dbt_dqm` execute:

```bash
dbt-dqm run
dbt-dqm run --select tag:customer --exclude tag:experimental --threads 4
dbt-dqm run --vars '{dbt_dqm_default_test_limit: 250}'
dbt-dqm run --summary
dbt-dqm run --verbose
```

In PowerShell, use `--vars "dbt_dqm_default_test_limit: 250"`. Paths containing spaces
should be quoted on either platform. `--verbose` displays complete dbt console output;
normal output shows phase progress, validation messages, warnings, one sorted line per test,
and a completion summary. dbt file logging remains available in both modes.

Test failures still produce captured evidence and reconciliation. Exit codes are `0` for a
successful workflow, `1` for failed tests, and `2` for configuration or execution errors.
A dbt `warn` result keeps dbt's successful exit behavior. App startup only reads metadata
and warehouse state; it does not run tests.

Limits apply to raw failed rows before issue deduplication. Reaching the exact limit is
ambiguous: the full failure count is unknown. The execution is marked
`collection_status=limit_reached`; observed failures are reconciled, but omitted issues
are not archived by absence. A later complete execution can resolve them. The completion
line reports the count of limited collections and names affected tests when positive.

The resolved artifact directory contains `dqm_workflow_metrics.json` with phase timings,
SQL counts, available BigQuery job IDs, collection limits, and owner-summary results when
requested. Available metrics are written even after an execution failure. Serialize DQM
workflows sharing stored-failure tables: equal counts do not prove two invocations read the
same rows. BigQuery allows one reconciliation per resolved installation at a time.

### Selection, state, and deferral

`--defer`, `--favor-state`, and `--state` can use production artifacts for upstream
references without building every upstream model in development. `--target-path` chooses
the artifact output directory:

```bash
dbt-dqm run --defer --favor-state --state prod-artifacts --target-path dev-artifacts
```

Explicit flags override `DBT_DEFER`, `DBT_FAVOR_STATE`, and `DBT_STATE`;
`--no-defer` and `--no-favor-state` override enabled environment settings. The runner
also inherits `DBT_DEFER_STATE` when no explicit deferral artifacts are given. Input state
and output artifacts must be different resolved directories; the command rejects a collision
before parsing can overwrite a state manifest. Deferral applies only to consumer test
compilation and execution. DQM setup, capture, reconciliation, and maintenance stay in the
selected target. See [operations](operations.md#project-target-and-artifact-resolution)
for the project, profile, target, and path defaults.

### Owner summary

`dbt-dqm run --summary` adds a table grouped by the saved owner shown in the app:

```text
Owner                 OPENED (NEW)  RECURRING  ARCHIVED  NEW (status)  ACTIVE
Total                           0        268         4           274     274
owner.a@example.com             0        252         4           258     258
owner.b@example.com             0         16         0            16      16
Unassigned                      0          0         0             0       0
```

`OPENED (NEW)`, `RECURRING`, and `ARCHIVED` count appearances, recurrences, and
Passed closures applied by the **current reconciliation**; structural closures are excluded.
They include previously pending captures applied now, not a calendar day's changes. An
occurrence can count in both opened and archived if both happened during that reconciliation.
`NEW (status)` counts active issues awaiting review under that workflow status; `ACTIVE`
counts all unarchived issues, including NEW. These two columns describe the current workload
across the installation. Unchanged failures add no change count.

Total appears first. Owners sort by `NEW (status)` descending, then name; Unassigned is
always present. Comma-separated owners stay one label, and owners with only archived changes
appear. The summary uses persisted assignments, including reviewer edits. Without the flag,
no owner-summary query runs. If reporting cannot read one consistent installation generation,
it warns without changing the workflow exit code.

### Naming variables

Package models and tracking objects follow the project's `generate_schema_name` and
`generate_alias_name` rules. If those macros use variables, supply the same `--vars`
to `run`, `app`, and `workspace` so they address one installation:

```bash
dbt-dqm run --vars '{table_alias_prefix: alice}' --summary
dbt-dqm app --vars '{table_alias_prefix: alice}'
dbt-dqm workspace info --vars '{table_alias_prefix: alice}'
```

Changing naming rules does not migrate old data. Read [upgrade and reset guidance](operations.md#upgrading)
before changing an installation's resolved namespace.
