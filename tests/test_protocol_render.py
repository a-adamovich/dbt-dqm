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


def render(protocol_project, monkeypatch, macro, arguments=""):
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
            "{{ dbt_dqm." + macro + "(" + arguments + ") }}",
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


def test_app_staging_migration_and_cleanup_render_without_raw_retention(protocol_project, monkeypatch):
    sql = render(protocol_project, monkeypatch, "setup_sql")
    assert "0004_app_staging_safety" in sql
    assert "alter column staged_at set default current_timestamp()" in sql
    assert "set staged_at=current_timestamp() where staged_at is null" in sql
    steps = render(protocol_project, monkeypatch, "maintenance_steps", "none, none")
    assert "staging_expiry" in steps and "raw_pruning" not in steps
    expiry = render(
        protocol_project,
        monkeypatch,
        "bigquery__maintenance_step_script",
        "{'name': 'staging_expiry', 'transactional': false}, none, none",
    )
    assert "dqm_app_change_staging" in expiry and "interval 24 hour" in expiry
    assert "delete from `compile-only`.`dqm`.`dqm_test_executions`" not in expiry


def step_script(protocol_project, monkeypatch, name, transactional, days="30"):
    return render(
        protocol_project,
        monkeypatch,
        "bigquery__maintenance_step_script",
        f"{{'name': '{name}', 'transactional': {str(transactional).lower()}}}, {days}, 365",
    )


def test_bigquery_maintenance_steps_report_outcomes_and_log_separately(
    protocol_project, monkeypatch
):
    raw = step_script(protocol_project, monkeypatch, "raw_pruning", True)
    # Each step job reports its own outcome; the log rows are written by a separate job.
    assert "select dqm_step_ok as ok, @@script.job_id as job_id," in raw
    assert raw.rstrip().endswith("as trigger_marker_at;")
    assert "dqm_maintenance_log" not in raw
    assert "begin transaction; set dqm_step_txn = true;" in raw
    assert "set dqm_step_txn = false; commit transaction;" in raw
    assert "if dqm_step_txn then\n      rollback transaction;" in raw
    log = render(
        protocol_project,
        monkeypatch,
        "bigquery__maintenance_log_script",
        "[{'step': 'raw_pruning', 'ok': false, 'job_id': 'job_1'}, "
        "{'step': 'log_pruning', 'ok': true, 'job_id': 'job_2'}], "
        "\"timestamp '2026-10-10T00:00:00.000000Z'\", 'automatic'",
    )
    # One job logs every outcome after the steps; each insert is protected on its own.
    assert log.count("insert into `compile-only`.`dqm`.`dqm_maintenance_log`") == 2
    assert log.count("exception when error then") == 2
    assert "trigger_marker_at,trigger_kind" in log and "'automatic'" in log
    # The first step's job reads the marker before its own work; later steps don't read it.
    first = render(
        protocol_project,
        monkeypatch,
        "bigquery__maintenance_step_script",
        "{'name': 'raw_pruning', 'transactional': true}, 30, 365, true",
    )
    assert first.index("set dqm_trigger_marker =") < first.index("begin transaction")
    assert "status='completed'" in first and "as trigger_marker_at;" in first
    assert "set dqm_trigger_marker =" not in raw


def test_bigquery_stage_drops_fail_the_step_but_recovery_stays_tolerant(
    protocol_project, monkeypatch
):
    drops = step_script(protocol_project, monkeypatch, "stage_drops", False)
    assert "set dqm_stage_failed = true;" in drops
    assert "raise using message = 'DQM stage drop failed'" in drops
    tolerant = render(protocol_project, monkeypatch, "cleanup_stages_sql")
    assert "Warning: DQM stage cleanup failed" in tolerant
    assert "raise" not in tolerant and "dqm_stage_failed" not in tolerant


def test_bigquery_run_pruning_keeps_runs_whose_stage_still_exists(protocol_project, monkeypatch):
    pruning = step_script(protocol_project, monkeypatch, "run_pruning", True)
    snapshot = pruning.index("create or replace temp table dqm_live_stages")
    assert "INFORMATION_SCHEMA.TABLES" in pruning
    # The stage list is read before the transaction opens; both deletes use it.
    assert snapshot < pruning.index("begin transaction")
    assert pruning.count("from dqm_live_stages live") == 2
    assert "concat('dqm_reconcile_stage_', replace(run.run_id, '-', ''))" in pruning


def test_bigquery_stage_is_created_by_the_freezing_job_with_an_expiry(
    protocol_project, monkeypatch
):
    # A run row becomes visible only when freeze commits. The stage DDL is in the same script, and
    # a script runs for at most 18 hours (with retries), so it always precedes the earliest
    # possible pruning of its run (at least a day after the run completes or is abandoned).
    sql = render(protocol_project, monkeypatch, "reconcile_pre")
    freeze = sql.index("commit transaction;")
    stage = re.search(r"create table .*dqm_reconcile_stage_[a-f0-9]+", sql).start()
    assert freeze < stage
    assert "expiration_timestamp=timestamp_add(current_timestamp(), interval 24 hour)" in sql


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


def test_duplicate_late_skip_is_rejected_before_warehouse_access(
    protocol_project, monkeypatch, capsys
):
    project, profiles = protocol_project

    def closed(cls, connection):
        connection.state = ConnectionState.CLOSED
        return connection

    def reject(*args, **kwargs):
        raise AssertionError("Duplicate skip attempted warehouse access")

    monkeypatch.setattr(BigQueryConnectionManager, "close", classmethod(closed))
    monkeypatch.setattr(BigQueryConnectionManager, "open", classmethod(reject))
    item = {"test_unique_id": "test.example.check", "invocation_id": "original-run"}
    result = dbtRunner().invoke(
        [
            "run-operation",
            "dqm_skip_late_evidence",
            "--args",
            json.dumps({"items": [item, item], "reason": "Reviewed delayed evidence"}),
            "--project-dir",
            str(project),
            "--profiles-dir",
            str(profiles),
        ]
    )
    assert not result.success
    assert "Late-evidence items must be distinct" in capsys.readouterr().out


def test_bigquery_native_grants_quote_roles_and_principals(protocol_project, monkeypatch):
    sql = render(
        protocol_project,
        monkeypatch,
        "native_grants_sql",
        "dbt_dqm.dqm_relation('dqm_issue_occurrences'), "
        "{'roles/bigquery.dataViewer': ['serviceAccount:demo@example.iam.gserviceaccount.com']}",
    )
    assert "grant `roles/bigquery.dataViewer` on TABLE" in sql
    assert "'serviceAccount:demo@example.iam.gserviceaccount.com'" in sql
    assert "revoke" not in sql.lower()
