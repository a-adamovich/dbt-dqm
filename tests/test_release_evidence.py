"""Regression checks for shared release evidence verification."""

import copy
import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
TREE = "a" * 40
SHA = "b" * 40
NOW = datetime(2026, 11, 7, 20, tzinfo=UTC)


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return (SimpleNamespace(REPOSITORY="a-adamovich/dbt-dqm"),
            importlib.import_module("release_evidence"))


@pytest.mark.parametrize("change", [None, "failed", "skipped", "cancelled", "missing", "tree", "stale", "workflow"])
def test_github_run_requires_all_jobs_and_actual_tree(modules, monkeypatch, change):
    local, gate = modules
    jobs = [{"name": name, "conclusion": "success"} for name in gate.ci_validation.full_jobs() | {"validated"}]
    run = {"id": 7, "run_attempt": 2, "repository": {"full_name": local.REPOSITORY},
           "head_sha": SHA, "workflow_id": 9, "path": ".github/workflows/full-validation.yml",
           "status": "completed", "conclusion": "success", "event": "workflow_dispatch",
           "run_started_at": (NOW - timedelta(hours=3)).isoformat()}
    if change in ("failed", "skipped", "cancelled"):
        next(job for job in jobs if job["name"] == "candidate-qa / memory-and-exports")["conclusion"] = change
    elif change == "missing":
        jobs.pop()
    elif change == "stale":
        run["run_started_at"] = (NOW - timedelta(days=15)).isoformat()
    elif change == "workflow":
        run["workflow_id"] = 10
    def api(*args):
        if args[-1].endswith("/runs/7"):
            return run
        if "/workflows/" in args[-1]:
            return {"id": 9}
        return [{"jobs": jobs}]
    monkeypatch.setattr(gate, "gh", api)
    monkeypatch.setattr(gate, "commit_tree", lambda *a: "d" * 40 if change == "tree" else TREE)
    def check():
        return gate.verify_run(local.REPOSITORY, "7", workflow="full-validation.yml",
            expected_jobs=gate.ci_validation.full_jobs() | {"validated"}, tree=TREE, now=NOW)
    if change is None:
        assert check()[0]["run_attempt"] == 2
    else:
        with pytest.raises(ValueError):
            check()


@pytest.mark.parametrize("change", ["expired", "run", "sha", "duplicate"])
def test_artifact_binding_rejects_stale_or_ambiguous_metadata(modules, monkeypatch, change):
    local, gate = modules
    run = {"id": 7, "head_sha": SHA}
    artifact = {"id": 9, "name": "warehouse-core-acceptance", "expired": False,
                "size_in_bytes": 100, "workflow_run": {"id": 7, "head_sha": SHA}}
    if change == "expired":
        artifact["expired"] = True
    elif change == "run":
        artifact["workflow_run"]["id"] = 6
    elif change == "sha":
        artifact["workflow_run"]["head_sha"] = "older"
    artifacts = [artifact, copy.deepcopy(artifact)] if change == "duplicate" else [artifact]
    monkeypatch.setattr(gate, "gh", lambda *args: [{"artifacts": artifacts}])
    with pytest.raises(ValueError):
        gate.artifact_files(local.REPOSITORY, run, "warehouse-core-acceptance")


def test_warehouse_checkout_must_match_run_sha_even_with_identical_tree(modules, monkeypatch):
    local, gate = modules
    record = {"schema_version": 1, "scope": "postgres-and-platform",
              "repository": local.REPOSITORY, "worktree_dirty": False,
              "run_id": "7", "run_attempt": "2", "sha": "c" * 40, "tree": TREE}
    monkeypatch.setattr(gate, "artifact_files", lambda *args: {
        "evidence.json": json.dumps(record), "junit.xml": "<testsuite/>"})
    monkeypatch.setattr(gate, "commit_tree", lambda *args: TREE)
    with pytest.raises(ValueError, match="checkout"):
        gate.warehouse_report(local.REPOSITORY, {"id": 7, "run_attempt": 2, "head_sha": SHA},
                              "warehouse-core-acceptance", TREE, core=True)


