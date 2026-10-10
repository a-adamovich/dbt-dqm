from concurrent.futures import Future
from unittest.mock import MagicMock

import psycopg2
from google.api_core.exceptions import NotFound
from streamlit.testing.v1 import AppTest

from dbt_dqm_app import warehouse
from dbt_dqm_app.config import AppConfig
from dbt_dqm_app.display import MAINTENANCE_STATES, maintenance_table
from dbt_dqm_app.store import Workspace


def config(tmp_path, adapter="postgres"):
    return AppConfig(
        tmp_path, tmp_path, "dev", "demo", adapter, "db", "schema", "US", "oauth", None
    )


def test_maintenance_states_are_labelled_and_unrecognized_means_unknown():
    rows = maintenance_table(
        [
            {"step": "staging_expiry", "state": "ok"},
            {"step": "raw_pruning", "state": "failing", "last_failure_diagnostic_id": "job_1"},
            {"step": "stage_drops", "state": "unknown"},
            {"step": "log_pruning", "state": None},
        ]
    )
    assert [row["state"] for row in rows] == [
        MAINTENANCE_STATES["ok"],
        MAINTENANCE_STATES["failing"],
        MAINTENANCE_STATES["unknown"],
        MAINTENANCE_STATES["unknown"],
    ]
    assert rows[0]["step"] == "staging expiry"
    assert rows[1]["diagnostic id"] == "job_1"
    assert maintenance_table(None) is None
    assert maintenance_table([]) == []


def test_fetch_health_tolerates_a_package_without_maintenance_health(tmp_path, monkeypatch):
    for missing in (NotFound("no view"), psycopg2.errors.UndefinedTable("no view")):

        def rows(config, query, missing=missing):
            if "dqm_maintenance_health" in query:
                raise missing
            return [{"value": 1}]

        monkeypatch.setattr(warehouse, "_query_rows", rows)
        health = warehouse.fetch_health(config(tmp_path))
        assert health["tests"] == [{"value": 1}] and health["maintenance"] is None


def test_maintenance_health_is_cached_with_the_rest_of_health(tmp_path):
    workspace = Workspace(tmp_path / "workspace.sqlite")
    maintenance = [{"step": "staging_expiry", "state": "unknown"}]
    workspace.replace_synced_data(
        [], {"tests": [], "areas": [], "maintenance": maintenance, "synced_at": "now"}
    )
    assert Workspace(tmp_path / "workspace.sqlite").health_rows()["maintenance"] == maintenance


def test_health_tab_shows_unknown_maintenance(tmp_path, monkeypatch):
    import dbt_dqm_app.streamlit_app as ui

    settings = config(tmp_path)
    path = tmp_path / "workspace.sqlite"
    monkeypatch.setattr(AppConfig, "workspace_path", property(lambda self: path))
    monkeypatch.setattr(ui, "load_config", lambda *args: settings)
    future = Future()
    future.set_result(
        {
            "issues": [],
            "health": {
                "tests": [],
                "areas": [],
                "maintenance": [{"step": "staging_expiry", "state": "unknown"}],
                "synced_at": "2026-10-10",
            },
        }
    )
    executor = MagicMock()
    executor.submit.return_value = future
    monkeypatch.setattr(ui, "_get_executor", lambda: executor)
    monkeypatch.setattr(ui, "manifest_nodes", lambda _: {})
    for name in ("DBT_DQM_PROJECT_DIR", "DBT_DQM_PROFILES_DIR"):
        monkeypatch.setenv(name, str(tmp_path))
    monkeypatch.setenv("DBT_DQM_TARGET", "dev")
    app = AppTest.from_string("from dbt_dqm_app.streamlit_app import run_app\nrun_app()").run()
    assert not app.exception
    assert any(header.value == "Maintenance" for header in app.subheader)
    table = next(frame.value for frame in app.dataframe if "state" in frame.value.columns)
    assert list(table["state"]) == [MAINTENANCE_STATES["unknown"]]
