# Quickstart on disposable BigQuery

Run the shipped [BigQuery demo](../integration_tests/demo_bigquery) against a dataset created solely for this walkthrough. Use the checkout root for installation commands.

## 1. Install the app and dbt 2

**Inspected.** Use separate environments for the app and dbt 2.0.11. The engine has a bundled BigQuery adapter and the app extra installs its own BigQuery client.

```bash
demo_env="$(mktemp -d)"
python -m venv "$demo_env/app"
source "$demo_env/app/bin/activate"
python -m pip install -e '.[bigquery]'
python -m venv "$demo_env/engine"
"$demo_env/engine/bin/python" -m pip install 'dbt==2.0.11'
export DBT_DQM_DBT_EXECUTABLE="$demo_env/engine/bin/dbt"
"$DBT_DQM_DBT_EXECUTABLE" --version
```

## 2. Prepare credentials and a dataset

**Needs authorized runtime verification.** Ask the project administrator to pre-create a uniquely named disposable dataset in `US` in your authorized Google Cloud project. The demo identity needs **BigQuery Job User** on the job project and **BigQuery Data Editor** on the demo dataset. Have the administrator confirm who will delete the dataset after the run. The shipped naming macro keeps the demo's failure tables in this dataset. [Create BigQuery datasets](https://docs.cloud.google.com/bigquery/docs/datasets)

**Needs authorized runtime verification.** Use keyless Application Default Credentials. For authorized local user credentials, run `gcloud auth application-default login`. With an approved federated identity, set `GOOGLE_APPLICATION_CREDENTIALS` to its trusted external-account configuration instead. Both the engine and the app use the `oauth` profile and need access to those credentials. [Authenticate to BigQuery](https://docs.cloud.google.com/bigquery/docs/authentication)

```bash
cd integration_tests/demo_bigquery
export DBT_PROFILES_DIR="$(mktemp -d)"
cp profiles.yml.example "$DBT_PROFILES_DIR/profiles.yml"
```

Set `project` and `dataset` in the copied profile to the exact project ID and disposable dataset name. Keep `location: US` to match the provisioned dataset. Verify the profile target, dataset ownership, and location before running the reset.

## 3. Open and review issues

**Needs authorized runtime verification.** Run the commands from the demo directory. The reset deletes tracking objects in the configured dataset.

```bash
"$DBT_DQM_DBT_EXECUTABLE" deps
"$DBT_DQM_DBT_EXECUTABLE" run-operation reset_demo_dqm
"$DBT_DQM_DBT_EXECUTABLE" seed
run_demo() {
  "$DBT_DQM_DBT_EXECUTABLE" run --select demo_records --vars "{demo_scenario: $1}" &&
  dbt-dqm run --vars "{demo_scenario: $1}" --summary &&
  "$DBT_DQM_DBT_EXECUTABLE" test --select assert_scenario_outcomes \
    --vars "{demo_scenario: $1, dqm_enabled: true}"
}
run_demo initial
```

Expect exit `0` for each command and `OPENED (NEW)` 7. The tracked tests report warnings, and the assertion checks for 2, 2, and 3 active occurrences across the three tests.

**Needs authorized runtime verification.** Start `dbt-dqm app` in a second shell with the same app environment, demo directory, `DBT_PROFILES_DIR`, and `DBT_DQM_DBT_EXECUTABLE`. Sync, set **Review verdict** to `TRUE_POSITIVE` for the `cust-001` identity, and add a note under **Edit annotations**. Leave the Notes field to stage it, then choose **Apply and sync**.

## 4. Pass, recur, and record usage

**Needs authorized runtime verification.** Run `run_demo passed`, Sync the app, and include archived issues to confirm the saved review. Then run `run_demo recurrence` and Sync again. Both scenarios and assertions should exit `0`. Expect `ARCHIVED` 7 after the pass and `RECURRING` 3 after the recurrence. Each test should have one active issue numbered 2. On the recurring customer issue, expect `NEW` and `UNREVIEWED`. Expand **Previous investigation** to see the earlier note and check the archived card for the prior verdict.

**Needs authorized runtime verification.** Save available job IDs from `dqm_workflow_metrics.json` and check the demo identity's job history for seed, model, assertion, and app jobs. Record the project, location, time window, job IDs, and bytes billed for on-demand queries or slot milliseconds for capacity-based usage. [BigQuery job metadata](https://docs.cloud.google.com/bigquery/docs/information-schema-jobs)

## 5. Tear down

**Needs authorized runtime verification.** Stop the app and use `dbt-dqm workspace purge` with the demo settings if you want to remove the local snapshot and drafts. Have the authorized dataset owner confirm the project and dataset ID, delete that dataset with its demo objects, and verify it is absent in the project as a principal with dataset visibility. Record the deletion and verification results. [Delete a BigQuery dataset](https://docs.cloud.google.com/bigquery/docs/managing-datasets)

## Offline check

**Ran.** Core compiled 27 nodes, including 16 data tests, with zero warehouse executions. The repository's offline check uses dbt Core 1.12.6 and dbt-bigquery 1.12.1 in a separate environment. From the checkout root run `uv sync --locked --group dbt1 --extra bigquery`. From the BigQuery demo directory, copy the unmodified example profile to a fresh temporary directory.

```bash
offline_profiles="$(mktemp -d)"
cp profiles.yml.example "$offline_profiles/profiles.yml"
uv run --no-sync dbt deps --profiles-dir "$offline_profiles"
uv run --no-sync python ../../scripts/compile_bigquery.py --profiles-dir "$offline_profiles"
```

The compile helper rejects warehouse connections. The [historical credentialed proof](bigquery-lifecycle.md#proof-runs-2026-11-11) documents dbt 2 coverage on its own revision.
