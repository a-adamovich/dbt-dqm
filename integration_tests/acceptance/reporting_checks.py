"""Shared real-warehouse checks for reconciliation changes and collection-limit reporting."""

import json
from datetime import UTC, datetime

from dbt_dqm_app.config import load_config
from dbt_dqm_app.relations import resolve_config
from dbt_dqm_app.store import Patch
from dbt_dqm_app.warehouse import apply_patches, fetch_issues


def check_run_reporting(demo, dedicated):
    project = getattr(demo, "project", None) or demo.path

    def run(**variables):
        result = dedicated(demo, "--summary", variables=variables)
        metrics = json.loads((project / "target/dqm_workflow_metrics.json").read_text())
        assert metrics["summary_status"] == "available", result.stdout + result.stderr
        report = metrics["owner_summary"]
        artifact = json.loads((project / "target/dqm_model_run_results.json").read_text())
        assert report["reconciliation_id"] == artifact["metadata"]["invocation_id"]
        assert all(label in result.stdout for label in (
            "OPENED (NEW)", "RECURRING", "ARCHIVED", "NEW (status)", "ACTIVE"))
        assert sum(row["active"] for row in report["owners"]) == report["active"]
        lines = [line for line in result.stdout.splitlines() if line.startswith("DQM test ")]
        # These fixture tests are all WARN (or all PASS in the empty source scenario).
        names = [line.split(": ", 2)[1] for line in lines]
        assert names == sorted(names, key=str.casefold)
        return report, metrics, result

    demo.dbt("run", "--select", "demo_records")
    first, metrics, result = run()
    assert first["opened"] == first["active"] == first["new"] == 7
    assert first["recurring"] == first["archived"] == 0
    assert metrics["collection"]["limit_reached_count"] == 0
    assert "0 collections reached the limit" in result.stdout

    config = resolve_config(load_config(project, demo.profiles, "dev"))
    issue = fetch_issues(config)[0]
    patches = [Patch(
        issue["occurrence_id"], field, issue[field], value, index,
        datetime.now(UTC).isoformat(), issue["annotation_version"],
        installation_id=issue["_installation_id"],
    ) for index, (field, value) in enumerate([
        ("dqm_owner", "reviewer-assigned"), ("workflow_status", "TRIAGED"),
    ], 1)]
    assert apply_patches(config, patches)
    limited, metrics, result = run(dbt_dqm_default_test_limit=2)
    assert limited["opened"] == limited["recurring"] == limited["archived"] == 0
    assert limited["active"] == 7 and limited["new"] == 6
    edited = next(row for row in limited["owners"] if row["owner"] == "reviewer-assigned")
    assert edited["active"] == 1 and edited["new"] == 0
    assert metrics["collection"]["limit_reached_count"] == 3
    assert len(metrics["collection"]["limit_reached_tests"]) == 3
    assert metrics["collection"]["captured_issue_count"] == 5
    assert "3 collections reached the limit" in result.stdout
    assert result.stdout.count("WARNING: Collections reached the limit:") == 1

    demo.dbt("run", "--select", "demo_records", variables={"demo_scenario": "passed"})
    closed, metrics, _ = run()
    assert closed["opened"] == closed["recurring"] == closed["active"] == closed["new"] == 0
    assert closed["archived"] == 7
    edited = next(row for row in closed["owners"] if row["owner"] == "reviewer-assigned")
    assert edited["archived"] == 1 and edited["active"] == 0
    assert metrics["collection"]["limit_reached_count"] == 0

    demo.dbt("run", "--select", "demo_records")
    reopened, _, _ = run()
    assert reopened["opened"] == reopened["archived"] == 0
    assert reopened["recurring"] == reopened["active"] == reopened["new"] == 7
