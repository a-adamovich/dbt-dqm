"""Compile both transaction protocols without making any warehouse calls."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
import yaml
from dbt.adapters.bigquery.connections import BigQueryConnectionManager
from dbt.adapters.contracts.connection import ConnectionState
from dbt.cli.main import dbtRunner

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def protocol_project(tmp_path_factory):
    path = tmp_path_factory.mktemp("compile-protocol")
    project = path / "project"
    shutil.copytree(
        ROOT / "integration_tests/demo_bigquery",
        project,
        ignore=shutil.ignore_patterns("target", "logs", "dbt_packages"),
    )
    (project / "dbt_packages").mkdir()
    (project / "dbt_packages/dbt_dqm").symlink_to(ROOT, target_is_directory=True)
    profiles = path / "profiles"
    profiles.mkdir()
    (profiles / "profiles.yml").write_text(
        yaml.safe_dump(
            {
                "dbt_dqm_demo": {
                    "target": "dev",
                    "outputs": {
                        "dev": {
                            "type": "bigquery",
                            "method": "oauth",
                            "project": "compile-only",
                            "dataset": "dqm",
                            "threads": 1,
                        }
                    },
                }
            }
        )
    )
    return project, profiles


def render(protocol_project, monkeypatch, macro):
    project, profiles = protocol_project

    def closed(cls, connection):
        connection.state = ConnectionState.CLOSED
        return connection

    def reject(*args, **kwargs):
        raise AssertionError("Compile attempted warehouse access")

    monkeypatch.setattr(BigQueryConnectionManager, "close", classmethod(closed))
    monkeypatch.setattr(BigQueryConnectionManager, "open", classmethod(reject))
    result = dbtRunner().invoke(
        [
            "--no-populate-cache",
            "compile",
            "--no-introspect",
            "--inline",
            "{{ dbt_dqm." + macro + "() }}",
            "--project-dir",
            str(project),
            "--profiles-dir",
            str(profiles),
        ]
    )
    assert result.success, result.exception
    return result.result[0].node.compiled_code


def test_bigquery_apply_fences_and_protects_annotations(protocol_project, monkeypatch):
    sql = render(protocol_project, monkeypatch, "apply_reconciliation")
    update = sql.split("when matched then update set", 1)[1].split("when not matched", 1)[0]
    for field in (
        "notes",
        "poc_responsible",
        "review_verdict",
        "annotation_version",
        "annotation_updated_at",
        "test_status",
    ):
        assert f"{field}=" not in update
    assert "then target.workflow_status else target.workflow_status_at_close" in update
    assert "run.generation_at_freeze=control.generation" in sql
    assert "run.status='started'" in sql
    assert "begin transaction" in sql and "commit transaction" in sql
    assert "dqm_reconciliation_receipts" in sql and "dqm_issue_events" in sql
    assert "drop table" not in sql
    assert "set generation=generation+1 where true;" in sql


def test_bigquery_runtime_stages_and_manifest_stay_stable(protocol_project, monkeypatch):
    first = render(protocol_project, monkeypatch, "reconcile_pre")
    project, _ = protocol_project
    manifest1 = json.loads((project / "target/manifest.json").read_text())
    second = render(protocol_project, monkeypatch, "reconcile_pre")
    manifest2 = json.loads((project / "target/manifest.json").read_text())
    stage1 = re.search(r"create table .*dqm_reconcile_stage_([a-f0-9]+)", first).group(1)
    stage2 = re.search(r"create table .*dqm_reconcile_stage_([a-f0-9]+)", second).group(1)
    assert stage1 != stage2
    for manifest in (manifest1, manifest2):
        node = manifest["nodes"]["model.dbt_dqm.dqm_reconcile"]
        assert node["alias"] == "dqm_reconcile" and node["config"]["materialized"] == "view"
        assert stage1 not in json.dumps(node["config"]) and stage2 not in json.dumps(node["config"])
    assert "create table if not exists `compile-only`.`dqm`.`dqm_install` as select" in first
    assert "dqm_migration_token" in first and "migration_lease_until" in first
    assert "interval 60 minute), generation=generation+1 where true;" in first
    assert "Late DQM evidence" in first
    assert "dqm_reconciliation_inputs" in first
