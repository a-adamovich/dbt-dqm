"""The scheduled BigQuery workflow stays keyless, pinned and confined to main (plan 1b)."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / ".github/workflows/bigquery-scheduled.yml").read_text()
WORKFLOW = yaml.safe_load(TEXT)


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
    assert "-n 6 --dist load integration_tests/acceptance/test_bigquery.py" in runs
    assert "bigquery_datasets.py" in runs
    assert "dbt_dqm_qa" in runs and "no access" in runs
