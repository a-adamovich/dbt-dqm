from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from dbt_dqm_app.config import AppConfig
from dbt_dqm_app.display import priority_rank, tag_list
from dbt_dqm_app.store import Workspace
from dbt_dqm_app.warehouse import _relation_parts, insert_missed_issue


def config(tmp_path, adapter="postgres"):
    return AppConfig(
        tmp_path, tmp_path, "dev", "demo", adapter, "database", "schema", "US", "oauth", None
    )


def test_optional_tags_and_priority():
    assert tag_list('["Demo", " high ", "Demo", null, 1]') == ["Demo", "high"]
    assert tag_list("broken") == []
    assert tag_list('{"priority":"high"}') == []
    assert [priority_rank(v) for v in ("CRITICAL", "high", "medium", "low", None)] == list(range(5))


def test_health_cache_survives_reopen_and_issue_sync(tmp_path):
    path = tmp_path / "workspace.sqlite"
    workspace = Workspace(path)
    health = {
        "tests": [{"reviewed_precision": None, "assessed_count": 0}],
        "areas": [],
        "synced_at": "today",
    }
    workspace.replace_health(health)
    workspace.replace_snapshot([])
    assert Workspace(path).health_rows() == health


def test_verdict_overlay_and_validation(tmp_path):
    workspace = Workspace(tmp_path / "workspace.sqlite")
    workspace.replace_snapshot(
        [{"occurrence_id": "one", "review_verdict": "UNREVIEWED", "annotation_version": 3}]
    )
    workspace.set_change("one", "review_verdict", "TRUE_POSITIVE")
    assert workspace.pending()[0].base_annotation_version == 3
    assert workspace.rows()[0]["review_verdict"] == "TRUE_POSITIVE"
    with pytest.raises(ValueError, match="verdict"):
        workspace.set_change("one", "review_verdict", "RESOLVED")


def test_hook_owned_relation_uses_reconcile_schema(tmp_path):
    import json

    (tmp_path / "target").mkdir()
    (tmp_path / "target/manifest.json").write_text(
        json.dumps(
            {
                "nodes": {
                    "model.dbt_dqm.dqm_reconcile": {
                        "database": "custom_db",
                        "schema": "custom_schema",
                        "alias": "reconcile",
                    }
                }
            }
        )
    )
    assert _relation_parts(config(tmp_path), "dqm_issue_occurrences") == (
        "custom_db",
        "custom_schema",
        "dqm_issue_occurrences",
    )


def test_missed_issue_postgres_uses_parameters_and_stable_id(tmp_path, monkeypatch):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value

    @contextmanager
    def connect(_):
        yield connection

    monkeypatch.setattr("dbt_dqm_app.warehouse._postgres_connection", connect)
    issue = {
        "missed_issue_id": "same-id",
        "description": "Robert'); drop table issues;--",
        "area_label": "Unmapped",
        "root_cause": "no_test",
    }
    for _ in range(2):
        assert insert_missed_issue(config(tmp_path), issue) == "same-id"
    sql, params = cursor.execute.call_args.args
    assert "on conflict(missed_issue_id) do nothing" in sql
    assert issue["description"] not in sql
    assert issue["description"] in params
    assert params[0] == "same-id"


def test_missed_issue_bigquery_uses_parameters(tmp_path, monkeypatch):
    from google.api_core.exceptions import NotFound

    client = MagicMock()
    client.get_job.side_effect = NotFound("not found")
    monkeypatch.setattr("dbt_dqm_app.warehouse.client_for", lambda _: client)
    issue = {
        "missed_issue_id": "id",
        "description": "'quote'",
        "source_unique_id": "model.example",
        "root_cause": "test_logic_gap",
    }
    insert_missed_issue(replace(config(tmp_path), adapter_type="bigquery"), issue)
    sql = client.query.call_args.args[0]
    assert "when not matched then insert" in sql
    assert issue["description"] not in sql
    params = client.query.call_args.kwargs["job_config"].query_parameters
    assert next(p.value for p in params if p.name == "description") == issue["description"]


def test_pending_issue_remains_available_outside_cache(tmp_path):
    workspace = Workspace(tmp_path / "workspace.sqlite")
    workspace.replace_snapshot([{"occurrence_id": "one", "notes": None, "annotation_version": 1}])
    workspace.set_change("one", "notes", "pending")
    workspace.replace_snapshot([])
    assert workspace.rows()[0]["notes"] == "pending"
    assert workspace.rows()[0]["_outside_cache"] is True
    workspace.clear_applied(workspace.pending())
    workspace.replace_snapshot([])
    assert workspace.rows() == []


def test_bigquery_missed_retry_reuses_completed_job(tmp_path, monkeypatch):
    from google.api_core.exceptions import NotFound

    client = MagicMock()
    job = MagicMock(state="DONE", error_result=None)
    client.get_job.side_effect = [NotFound("not found"), job]
    client.query.return_value = job
    monkeypatch.setattr("dbt_dqm_app.warehouse.client_for", lambda _: client)
    issue = {
        "missed_issue_id": "stable",
        "description": "Known miss",
        "area_label": "Area",
        "root_cause": "other",
    }
    for _ in range(2):
        insert_missed_issue(replace(config(tmp_path), adapter_type="bigquery"), issue)
    assert client.query.call_count == 1
    assert client.query.call_args.kwargs["job_id"] == client.get_job.call_args.args[0]
    assert client.query.call_args.kwargs["job_retry"] is None
