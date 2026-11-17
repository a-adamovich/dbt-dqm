# Quickstart in an existing dbt project

Pilot one test in a development target using synthetic data you can change. Choose the install for your warehouse. The worked lifecycle below was run on Postgres with `pgcrypto` enabled.

## 1. Install the dbt package and Python command

**Ran on Postgres.** From the consumer project, add the Git package to `packages.yml`.

```yaml
packages:
  - git: https://github.com/a-adamovich/dbt-dqm.git
    revision: v0.2.5
```

### Postgres with dbt Core

**Ran.** Activate your project's Python environment first.

```bash
# Example for a fresh Postgres environment
uv pip install 'dbt-core==1.12.6' 'dbt-postgres==1.11.0' 'dbt-dqm[postgres]==0.2.5'
dbt deps
```

### BigQuery with dbt 2

**Inspected / needs authorized runtime verification.** In your app environment, install the
BigQuery client extra. Keep dbt 2.0.11 in a separate engine environment and use your existing
BigQuery development profile and authorized credentials.

```bash
uv pip install 'dbt-dqm[bigquery]==0.2.5'
engine_env="$PWD/.venv-dbt2"
uv venv "$engine_env"
uv pip install --python "$engine_env/bin/python" 'dbt==2.0.11'
export DBT_DQM_DBT_EXECUTABLE="$engine_env/bin/dbt"
"$DBT_DQM_DBT_EXECUTABLE" deps
```

Use that executable for ordinary dbt commands below. The app and DQM runner use the exported
path, which you can also supply with `--dbt-executable`. If you already have a separate dbt 2.0.11
environment, set the variable to its executable and run `deps` there. Keep the engine
environment out of version control. See
[supported configurations](supported-configurations.md) for the engine coverage.

### Configure retention and target

Add these variables to your existing `vars:` mapping in `dbt_project.yml`.

```yaml
vars:
  dbt_dqm_retention_days: 30
  dbt_dqm_event_retention_days: 90
```

Use the same development profile and target throughout. The examples assume the profile defaults to that target. Otherwise add `--target dev` to each command, replacing `dev` with your development target name.

## 2. Preserve the ordinary CI test

**Ran.** For this pilot, create `models/pilot_customers.sql` with a row you control.

```sql
select 1 as customer_id, cast('good@example.test' as {{ dbt.type_string() }}) as email
union all
select 2 as customer_id, cast(null as {{ dbt.type_string() }}) as email
```

Keep the ordinary `not_null` test and add a second, separately named copy with DQM
configuration in the model's YAML. For SQL tests, see the
[singular-test example](reference-configuration.md#tracked-data-tests).

```yaml
version: 2
models:
  - name: pilot_customers
    columns:
      - name: email
        data_tests:
          - not_null
          - not_null:
              name: dqm_pilot_email_not_null
              config:
                enabled: "{{ var('dqm_enabled', false) }}"
                store_failures: true
                severity: error
                limit: 100
                meta:
                  dbt_dqm:
                    granularity: ['customer_id']
                    owner: pilot_team
```

```bash
dbt ls --select pilot_customers --resource-type test
dbt build --select pilot_customers
dbt test --select not_null_pilot_customers_email
```

**Adding the enable guard to the original test would disable it in ordinary `dbt test` and
`dbt build` runs.** Leave the original test as it is and put the guard on the named DQM copy.

The listing shows the original test. With the synthetic null email, both ordinary CI commands exit `1` because the original test fails. The named copy stays disabled because `dqm_enabled` defaults to false. Keep the existing CI job and run the DQM copy in a separate job after building upstream models.

## 3. Open and review an issue

**Ran.** With the synthetic model built, run the DQM copy. Use this same selector for all three phases.

```bash
dbt-dqm run --select dqm_pilot_email_not_null --summary
```

Expect exit `1` and `OPENED (NEW)` 1. The quality failure has been captured. When scripting the walkthrough, check the expected exit `1` explicitly before continuing.

**Ran.** Start `dbt-dqm app` on the same target. Choose **Sync**, open the issue for `customer_id` 2, set **Review verdict** to `TRUE_POSITIVE`, and add the note `Confirmed missing email` under **Edit annotations**. Leave the Notes field to stage it, then select **Apply and sync** to persist the review.

## 4. Pass, then recur

**Ran.** Replace `null` in the model with `'fixed@example.test'`. Rebuild and rerun.

```bash
dbt run --select pilot_customers
dbt-dqm run --select dqm_pilot_email_not_null --summary
```

Expect exit `0` and `ARCHIVED` 1. Restore the original `null` and repeat those two commands. Expect exit `1` and `RECURRING` 1.

**Ran.** Sync after each phase. Include archived issues to see occurrence 1, closed as Passed with its verdict and note intact. The active occurrence 2 has status `NEW`, verdict `UNREVIEWED`, and the earlier note under **Previous investigation**. The archived card retains the earlier verdict, which also appears in the active row's `previous_review_verdict` in `dqm_all_issues`.

**Ran.** When finished with the synthetic pilot, stop the app. You can purge its local snapshot and drafts with `dbt-dqm workspace purge` on the same target. See [configuration](reference-configuration.md) for choosing granularity and setting capture limits, and [app workflows](review-app.md) for reviewing and exporting saved issues.
