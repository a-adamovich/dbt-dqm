"""Warehouse-mode review UI: paging, filters, search refusal, generation changes, auto mode."""

from concurrent.futures import Future
from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from streamlit.testing.v1 import AppTest

from dbt_dqm_app.config import AppConfig
from dbt_dqm_app.errors import SearchTooCostly
from dbt_dqm_app.limits import CacheLimitExceeded
from dbt_dqm_app.store import Workspace
from dbt_dqm_app.warehouse import Page

SCRIPT = "from dbt_dqm_app.streamlit_app import run_app\nrun_app()"
HEALTH = {"tests": [], "areas": [], "synced_at": "2026-10-11"}


def issue(number, status="NEW"):
    return {
        "occurrence_id": f"id{number:03}", "test_name": f"test {number}", "unique_id": "u",
        "record_status": "Active", "record_values_json": '{"k": 1}', "workflow_status": status,
        "review_verdict": "UNREVIEWED", "annotation_version": 0,
        "first_seen_at": "2026-10-01T00:00:00", "annotation_updated_at": None,
        "poc_responsible": None, "call_to_action": None, "notes": None, "ticket_url": None,
        "test_priority": "high", "test_tags": '["finance"]', "is_recurrence": False,
    }


class FakeWarehouse:
    """Pages over an in-memory list; the cursor is simply the next offset."""

    def __init__(self, issues):
        self.issues, self.generation, self.reads, self.refuse_search = issues, 1, [], False

    def matching(self, filters):
        return [row for row in self.issues
                if not filters.statuses or row["workflow_status"] in filters.statuses]

    def page(self, config, population, filters, cursor, page_size):
        if filters.search and self.refuse_search:
            raise SearchTooCostly("This search would process too much.")
        self.reads.append((filters, cursor))
        rows = self.matching(filters)
        start = cursor or 0
        chunk = [dict(row) for row in rows[start:start + page_size]]
        more = start + page_size < len(rows)
        return Page(chunk, start + page_size if more else None, self.generation)

    def summary(self, config, population, filters):
        if filters.search and self.refuse_search:
            raise SearchTooCostly("This search would process too much.")
        return {"active_count": len(self.issues), "new_count": 1, "blocked_count": 0,
                "recurring_count": 0, "matching": len(self.matching(filters)),
                "generation": self.generation}

    def options(self, config, population):
        return {"statuses": ["NEW", "BLOCKED"], "owners": ["Unassigned"], "tags": ["finance"],
                "first_seen_min": "2026-10-01T00:00:00", "first_seen_max": "2026-10-01T00:00:00",
                "generation": self.generation}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    import dbt_dqm_app.streamlit_app as ui

    def start(browse_mode="warehouse", futures=None, adapter="postgres", issues=60):
        config = AppConfig(tmp_path, tmp_path, "dev", "demo", adapter, "db", "schema", "US",
                           "oauth", None, browse_mode=browse_mode)
        path = tmp_path / "workspace.sqlite"
        monkeypatch.setattr(AppConfig, "workspace_path", property(lambda self: path))
        monkeypatch.setattr(ui, "load_config", lambda *args: config)
        monkeypatch.setattr(ui, "manifest_nodes", lambda _: {})
        for name in ("PROJECT_DIR", "PROFILES_DIR", "TARGET"):
            monkeypatch.setenv("DBT_DQM_" + name, "dev" if name == "TARGET" else str(tmp_path))
        warehouse = FakeWarehouse(
            [issue(i, "BLOCKED" if i % 3 == 0 else "NEW") for i in range(issues)])
        monkeypatch.setattr(ui, "browse_page", warehouse.page)
        monkeypatch.setattr(ui, "browse_summary", warehouse.summary)
        monkeypatch.setattr(ui, "browse_options", warehouse.options)
        monkeypatch.setattr(ui, "browse_population", lambda config: "true")
        health = Future()
        health.set_result({"health": HEALTH})
        executor = MagicMock()
        executor.submit.side_effect = [*(futures or []), *[health] * 20]
        monkeypatch.setattr(ui, "_get_executor", lambda: executor)
        app = AppTest.from_string(SCRIPT, default_timeout=30).run()
        assert not app.exception, app.exception
        return app, warehouse, executor, Workspace(path)

    return start


def card_titles(app):
    return [header.value for header in app.subheader if header.value.startswith("test ")]


def click(app, label):
    next(button for button in app.button if button.label == label).click().run()
    assert not app.exception, app.exception


def test_pages_forward_and_back_and_filters_restart_paging(harness):
    app, warehouse, executor, _ = harness()
    assert card_titles(app) == [f"test {i}" for i in range(25)]
    assert any("Page 1 · showing 25 of 60" in caption.value for caption in app.caption)
    click(app, "Next")
    assert card_titles(app)[0] == "test 25"
    click(app, "Next")
    assert card_titles(app) == [f"test {i}" for i in range(50, 60)]
    assert next(b for b in app.button if b.label == "Next").disabled
    click(app, "Previous")
    assert card_titles(app)[0] == "test 25"
    reads = len(warehouse.reads)
    click(app, "First")
    assert card_titles(app)[0] == "test 0" and len(warehouse.reads) == reads  # cached page
    click(app, "Next")
    next(box for box in app.multiselect if box.label == "Workflow status").select("BLOCKED").run()
    assert card_titles(app)[:2] == ["test 0", "test 3"]  # back on page 1 of the new filter
    # Only Health ran in the background; no full snapshot sync.
    assert {call.args[0].__name__ for call in executor.submit.call_args_list} == {
        "browse_refresh_worker"}


def test_an_edit_on_page_two_survives_navigation_and_applies(harness):
    app, _, executor, workspace = harness()
    click(app, "Next")
    verdict = next(box for box in app.selectbox if box.label == "Review verdict")
    verdict.set_value("TRUE_POSITIVE").run()
    assert [patch.occurrence_id for patch in workspace.pending()] == ["id025"]
    click(app, "Previous")
    click(app, "Next")
    assert "Unsaved local changes" in next(c.value for c in app.caption if "Active" in c.value)
    click(app, "Apply and sync")
    apply_call = executor.submit.call_args_list[-1]
    assert apply_call.args[0].__name__ == "apply_worker" and apply_call.args[3] is True


def test_pending_only_shows_edits_from_any_page(harness):
    app, _, _, _ = harness()
    click(app, "Next")
    next(box for box in app.selectbox if box.label == "Review verdict").set_value(
        "FALSE_POSITIVE").run()
    click(app, "First")
    next(box for box in app.checkbox if box.label == "Pending edits only").check().run()
    assert card_titles(app) == ["test 25"]


def test_a_generation_change_is_shown_with_a_restart(harness):
    app, warehouse, _, _ = harness()
    warehouse.generation = 2
    click(app, "Next")
    assert any("warehouse changed" in warning.value for warning in app.warning)
    click(app, "Restart from first page")
    assert not any("warehouse changed" in warning.value for warning in app.warning)
    assert card_titles(app)[0] == "test 0"


def test_a_refused_search_keeps_the_current_results(harness):
    app, warehouse, _, _ = harness()
    warehouse.refuse_search = True
    app.text_input[0].set_value("customer")
    next(button for button in app.button if button.label == "Apply").click().run()
    assert not app.exception, app.exception
    assert any("wasn't run" in warning.value for warning in app.warning)
    assert card_titles(app)[0] == "test 0"
    assert app.session_state["applied_search"] == ""


def test_a_submitted_search_reads_the_warehouse(harness):
    app, warehouse, _, _ = harness()
    app.text_input[0].set_value("customer")
    next(button for button in app.button if button.label == "Apply").click().run()
    assert warehouse.reads[-1][0].search == "customer"


def test_a_too_short_search_is_not_applied(harness):
    app, warehouse, _, _ = harness()
    app.text_input[0].set_value("ab")
    next(button for button in app.button if button.label == "Apply").click().run()
    assert any("3 to 200" in warning.value for warning in app.warning)
    assert all(filters.search is None for filters, _ in warehouse.reads)


def test_auto_mode_switches_to_warehouse_paging_above_the_snapshot_limits(harness):
    too_many = Future()
    too_many.set_exception(CacheLimitExceeded("Issue cache limit exceeded: 80,000 issues"))
    app, _, executor, _ = harness("auto", [too_many])
    for _ in range(3):
        app.run()
    assert app.session_state["browsing"] is True
    assert card_titles(app)[:1] == ["test 0"]
    workers = [call.args[0].__name__ for call in executor.submit.call_args_list]
    assert workers[:2] == ["sync_worker", "browse_refresh_worker"]


def test_auto_mode_is_the_default(tmp_path):
    from dbt_dqm_app.cli import parser

    config = AppConfig(tmp_path, tmp_path, "dev", "demo", "postgres", "db", "s", "US", "oauth", None)
    assert config.browse_mode == "auto"
    args = parser().parse_args(["app", "--project-dir", "p", "--profiles-dir", "p", "--target", "t"])
    assert args.browse == "auto"
    with pytest.raises(ValueError, match="--browse"):
        replace(config, browse_mode="sometimes")


def test_an_unreachable_warehouse_shows_the_cached_pages_offline(harness, monkeypatch):
    import dbt_dqm_app.streamlit_app as ui
    from dbt_dqm_app import warehouse as real

    app, warehouse, _, workspace = harness()
    click(app, "Next")
    next(box for box in app.selectbox if box.label == "Review verdict").set_value(
        "TRUE_POSITIVE").run()
    # The real page read against a closed port: a genuine connection failure.
    config = ui.load_config()
    monkeypatch.setattr(ui, "load_config", lambda *args: replace(config, host="127.0.0.1", port=1))
    monkeypatch.setattr(ui, "browse_page", real.browse_page)
    monkeypatch.setattr(ui, "browse_summary", real.browse_summary)
    app.session_state["browse_pages"] = {}
    app.session_state["browse_summaries"] = {}
    app.run()
    assert not app.exception, app.exception
    assert any("Offline" in warning.value for warning in app.warning)
    assert any(caption.value.startswith("Warehouse error:") for caption in app.caption)
    # Both loaded pages are available offline, with local filters; the edit is kept.
    assert any("of 50 issue record(s)" in caption.value for caption in app.caption)
    assert workspace.pending()[0].occurrence_id == "id025"
    assert next(b for b in app.button if b.label == "Apply and sync").disabled
    # Back online: Retry reads the warehouse again.
    monkeypatch.setattr(ui, "load_config", lambda *args: config)
    monkeypatch.setattr(ui, "browse_page", warehouse.page)
    monkeypatch.setattr(ui, "browse_summary", warehouse.summary)
    click(app, "Retry the warehouse")
    assert not any("Offline" in warning.value for warning in app.warning)
    assert not next(b for b in app.button if b.label == "Apply and sync").disabled


def test_bigquery_reads_a_chunk_and_pages_through_it_locally(harness):
    app, warehouse, _, workspace = harness(adapter="bigquery", issues=2_100)
    assert card_titles(app)[0] == "test 0"
    assert [(cursor, len(warehouse.reads)) for _, cursor in warehouse.reads] == [(None, 1)]
    for _ in range(79):  # pages 2..80 come from the first chunk of 2,000
        click(app, "Next")
    assert card_titles(app)[-1] == "test 1999" and len(warehouse.reads) == 1
    click(app, "Next")  # page 81 starts the second chunk
    assert card_titles(app)[0] == "test 2000" and warehouse.reads[-1][1] == 2_000
    assert any("Page 81" in caption.value for caption in app.caption)
    for _ in range(3):
        click(app, "Next")
    assert card_titles(app)[-1] == "test 2099" and len(warehouse.reads) == 2
    assert next(b for b in app.button if b.label == "Next").disabled
    for _ in range(4):  # back into the first chunk, still listed
        click(app, "Previous")
    assert card_titles(app)[-1] == "test 1999"
    # Rows trimmed from the local cache since the chunk was read are read again, not dropped.
    reads = len(warehouse.reads)
    with workspace.connect() as connection:
        connection.execute("delete from snapshot where occurrence_id='id1999'")
    click(app, "First")
    click(app, "Next")
    assert card_titles(app)[1] == "test 26"
    for _ in range(78):
        click(app, "Next")
    assert card_titles(app)[-1] == "test 1999" and len(warehouse.reads) == reads + 1
