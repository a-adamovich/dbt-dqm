"""The BigQuery workflow stays manual, keyless, pinned and confined to main."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / ".github/workflows/bigquery-lifecycle.yml").read_text()
WORKFLOW = yaml.safe_load(TEXT)


def test_workflow_is_started_manually_only():
    # PyYAML interprets the YAML 1.1 key "on" as True.
    assert WORKFLOW.get("on", WORKFLOW.get(True)) == {"workflow_dispatch": None}


def test_keyless_authentication_through_a_pinned_action():
    assert "KEYFILE" not in TEXT and "secrets." not in TEXT
    job = WORKFLOW["jobs"]["lifecycle"]
    auth = next(step for step in job["steps"] if "google-github-actions/auth" in step.get("uses", ""))
    ref = auth["uses"].partition("@")[2]
    assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref)  # a commit SHA, not a tag
    assert set(auth["with"]) == {"workload_identity_provider", "service_account", "export_environment_variables"}
    assert WORKFLOW["permissions"]["id-token"] == "write"


def test_runs_only_from_main_when_enabled():
    condition = WORKFLOW["jobs"]["lifecycle"]["if"]
    assert "vars.BIGQUERY_LIFECYCLE_ENABLED == 'true'" in condition
    assert "github.ref == 'refs/heads/main'" in condition


def test_runs_the_parallel_suite_sweeps_and_checks_qa_is_unreachable():
    runs = "\n".join(step.get("run", "") for step in WORKFLOW["jobs"]["lifecycle"]["steps"])
    assert '-n "$workers" --dist load integration_tests/acceptance/test_bigquery.py' in runs
    assert "workers=6\n" in runs  # dbt 2 full selection, as validated
    assert 'if [[ "${{ matrix.engine }}" == dbt1 ]]; then workers=4; fi' in runs
    assert 'if [[ "${{ matrix.engine }}" == dbt1 ]]; then selection=(-m bigquery_smoke); fi' in runs
    assert 'test_bigquery.py "${selection[@]}"' in runs
    assert "bigquery_datasets.py" in runs
    assert "dbt_dqm_qa" in runs and "no access" in runs


def job_auth(job):
    return [step for step in WORKFLOW["jobs"][job]["steps"] if "google-github-actions/auth" in step.get("uses", "")]


def test_gate_jobs_run_serially_after_the_matrix_and_only_from_main():
    jobs = WORKFLOW["jobs"]
    assert jobs["cross-engine-upgrade"]["needs"] == "lifecycle"
    assert jobs["restricted-permissions"]["needs"] == "cross-engine-upgrade"
    for name in ("cross-engine-upgrade", "restricted-permissions"):
        assert jobs[name]["if"] == jobs["lifecycle"]["if"]
        assert all("-n " not in step.get("run", "") for step in jobs[name]["steps"])
    assert {step["with"]["name"] for step in jobs["restricted-permissions"]["steps"] if "upload-artifact" in step.get("uses", "")} == {"restricted-bigquery-results"}
    assert {step["with"]["name"] for step in jobs["cross-engine-upgrade"]["steps"] if "upload-artifact" in step.get("uses", "")} == {"cross-engine-bigquery-results"}


def test_upgrade_job_can_read_history_and_records_both_engines():
    steps = WORKFLOW["jobs"]["cross-engine-upgrade"]["steps"]
    assert steps[0]["with"]["fetch-depth"] == 0
    runs = "\n".join(step.get("run", "") for step in steps)
    assert "DBT_DQM_UPGRADE_CORE_EXECUTABLE=" in runs and "DBT_DQM_UPGRADE_V2_EXECUTABLE=" in runs
    assert "--kind upgrade --engine dbt2" in runs and "--core-executable" in runs


def test_restricted_job_keeps_three_keyless_identities_separate():
    auths = job_auth("restricted-permissions")
    assert [step["id"] for step in auths] == ["provisioner", "runner", "reviewer"]
    assert [step["with"]["service_account"] for step in auths] == [
        "${{ vars.GCP_CI_SERVICE_ACCOUNT }}", "${{ vars.GCP_CI_RUNNER_SERVICE_ACCOUNT }}",
        "${{ vars.GCP_CI_REVIEWER_SERVICE_ACCOUNT }}"]
    for step in auths:
        assert set(step["with"]) == {"workload_identity_provider", "service_account", "export_environment_variables"}
    test = next(step for step in WORKFLOW["jobs"]["restricted-permissions"]["steps"]
                if step.get("name") == "Restricted runner and reviewer on a disposable dataset")
    for role in ("provisioner", "runner", "reviewer"):
        assert test["env"][f"DBT_DQM_BIGQUERY_{role.upper()}_CREDENTIALS"] == (
            "${{ steps." + role + ".outputs.credentials_file_path }}")


def test_no_identity_is_exported_implicitly():
    for name, job in WORKFLOW["jobs"].items():
        auths = job_auth(name)
        assert auths and all(step["with"]["export_environment_variables"] is False for step in auths)
        own = {"${{ steps." + step["id"] + ".outputs.credentials_file_path }}" for step in auths}
        for step in job["steps"]:
            for value in step.get("env", {}).values():
                assert "credentials_file_path" not in str(value) or value in own


def test_restricted_job_selects_each_identity_explicitly():
    steps = WORKFLOW["jobs"]["restricted-permissions"]["steps"]
    assert all("GITHUB_ENV" not in step.get("run", "") for step in steps)
    provisioner = "${{ steps.provisioner.outputs.credentials_file_path }}"
    for step in steps:
        selected = step.get("env", {}).get("GOOGLE_APPLICATION_CREDENTIALS")
        assert selected is None or (step["name"] == "Sweep stale acceptance datasets" and selected == provisioner)
    test = next(step for step in steps if step.get("name") == "Restricted runner and reviewer on a disposable dataset")
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in test["env"]


def test_documented_trust_is_pinned_to_ids_this_workflow_and_manual_dispatch():
    guide = (ROOT / "docs/bigquery-lifecycle.md").read_text()
    for clause in ("assertion.repository_id=='$REPOSITORY_ID'", "assertion.repository_owner_id=='$OWNER_ID'",
                   "assertion.workflow_ref=='$WORKFLOW_REF'", "assertion.ref=='refs/heads/main'",
                   "assertion.event_name=='workflow_dispatch'",
                   'WORKFLOW_REF="a-adamovich/dbt-dqm/.github/workflows/bigquery-lifecycle.yml@refs/heads/main"'):
        assert clause in guide
    assert "attribute.repository/" not in guide
    assert guide.count("attribute.repository_id/$REPOSITORY_ID") == 1  # one loop binds all three accounts
    assert "POOL=dbt-dqm-lifecycle" in guide and "workload-identity-pools create github" not in guide


def test_documented_roles_are_minimal_and_rollback_covers_runs():
    guide = (ROOT / "docs/bigquery-lifecycle.md").read_text()
    setup = guide[guide.index("### 4. Grant the minimum project roles"):guide.index("### 5.")]
    assert setup.count("--role=roles/bigquery.user") == 1 and setup.count("--role=roles/bigquery.jobUser") == 1
    assert setup.index("dbt-dqm-ci@") < setup.index("--role=roles/bigquery.jobUser")  # jobUser only for runner and reviewer
    for role in ("dataViewer", "dataEditor", "dataOwner", "roles/bigquery.admin"):
        assert role not in setup
    rollback = guide[guide.index("## Rollback"):]
    assert "### A. Undo setup before any run" in rollback and "### B. Clean up after a run" in rollback
    assert rollback.index('--filter="states:pending,running"') < rollback.index("bq rm -r -f -d")
    assert rollback.index("bq rm -r -f -d") < rollback.index("4. Undo the setup with case A")
    assert "### Optional: disable STS" in rollback
    assert "Residual limitation, for maintainer review" in guide
