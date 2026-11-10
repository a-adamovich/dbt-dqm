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
    assert set(auth["with"]) == {"workload_identity_provider", "service_account"}
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
