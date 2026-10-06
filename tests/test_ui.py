from concurrent.futures import Future
from unittest.mock import MagicMock

from streamlit.testing.v1 import AppTest

from dbt_dqm_app.config import AppConfig
from dbt_dqm_app.store import Workspace


def test_review_health_and_missed_retry_ui(tmp_path, monkeypatch):
    import dbt_dqm_app.streamlit_app as ui

    config = AppConfig(
        tmp_path, tmp_path, "dev", "demo", "postgres", "db", "schema", "US", "oauth", None
    )
    path = tmp_path / "workspace.sqlite"
    monkeypatch.setattr(AppConfig, "workspace_path", property(lambda self: path))
    monkeypatch.setattr(ui, "load_config", lambda *args: config)
    issue = {
        "occurrence_id": "one",
        "unique_id": "id",
        "test_name": "Test customer",
        "record_status": "Active",
        "record_values_json": '{"customer_id":"CUST-1"}',
        "workflow_status": "NEW",
        "review_verdict": "UNREVIEWED",
        "annotation_version": 0,
        "first_seen_at": "2026-10-06T00:00:00",
        "annotation_updated_at": None,
        "poc_responsible": None,
        "call_to_action": None,
        "notes": None,
        "ticket_url": None,
        "test_priority": "high",
        "test_criticality": "Customer contact",
        "test_tags": '["Customer"]',
        "has_previous_occurrence": False,
        "is_recurrence": False,
    }
    future = Future()
    future.set_result(
        {
            "issues": [issue],
            "health": {
                "tests": [
                    {
                        "assessed_count": 0,
                        "reviewed_precision": None,
                        "reporting_window_days": 30,
                        "attention_flags": "",
                    }
                ],
                "areas": [],
                "synced_at": "2026-10-06",
            },
        }
    )
    executor = MagicMock()
    executor.submit.return_value = future
    monkeypatch.setattr(ui, "_get_executor", lambda: executor)
    monkeypatch.setattr(ui, "manifest_nodes", lambda _: {})
    monkeypatch.setenv("DBT_DQM_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("DBT_DQM_PROFILES_DIR", str(tmp_path))
    monkeypatch.setenv("DBT_DQM_TARGET", "dev")
    app = AppTest.from_string("from dbt_dqm_app.streamlit_app import run_app\nrun_app()").run()
    assert not app.exception
    assert [tab.label for tab in app.tabs] == ["Issues", "Health", "Report missed issue"]
    verdict = next(box for box in app.selectbox if box.label == "Review verdict")
    verdict.set_value("TRUE_POSITIVE").run()
    assert not app.exception
    assert Workspace(path).pending()[0].field_name == "review_verdict"
    assert Workspace(path).health_rows()["tests"][0]["assessed_count"] == 0
    # A failed confirmation preserves the ID, and a retry reuses it.
    insert = MagicMock(side_effect=[RuntimeError("lost response"), "confirmed"])
    monkeypatch.setattr(ui, "insert_missed_issue", insert)
    next(field for field in app.text_input if field.label.startswith("Area name")).set_value(
        "Unknown area"
    )
    next(field for field in app.text_area if field.label == "Description").set_value(
        "Known missed issue"
    )
    submission = app.session_state["missed_submission_id"]
    next(button for button in app.button if button.label == "Report missed issue").click().run()
    assert not app.exception
    assert app.session_state["missed_submission_id"] == submission
    next(button for button in app.button if button.label == "Report missed issue").click().run()
    assert not app.exception
    assert [call.args[1]["missed_issue_id"] for call in insert.call_args_list] == [
        submission,
        submission,
    ]
    assert app.session_state["missed_submission_id"] != submission
