"""Configuration and isolation boundaries for the dedicated DQM command."""
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from dbt_dqm_app import runner
from dbt_dqm_app.cli import parser
from dbt_dqm_app.errors import AppError
from dbt_dqm_app.store import Workspace


def node(**overrides):
    config = {"meta": {"dbt_dqm": {"granularity": ["customer_id"]}}, "store_failures": True,
              "limit": None, "fail_calc": "count(*)", "store_failures_as": None}
    config.update(overrides)
    return SimpleNamespace(config=SimpleNamespace(**config))


@pytest.mark.parametrize("limit", [None, 1, 100, 10_000])
def test_positive_native_limits_and_default_are_accepted(limit):
    assert runner.validate_test(node(limit=limit), 100) == []


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "100"])
def test_invalid_limits_are_rejected_before_reset(limit):
    assert "native test limit must be a positive integer" in runner.validate_test(node(limit=limit), 100)


@pytest.mark.parametrize("metadata", [
    {"dbt_dqm": None}, {"dbt_dqm": {}}, {"dbt_dqm": {"granularity": "id"}},
    {"dbt_dqm": {"granularity": ["ID", " id "]}},
    {"dbt_dqm": {"granularity": ["id"], "owner_column": "owner"}},
    {"dbt_dqm": {"granularity": ["id"], "poc_responsible": "owner"}},
    {"dbt_dqm": {"granularity": ["id"]}, "dqm_granularity": ["id"]},
])
def test_required_metadata_and_obsolete_aliases(metadata):
    assert runner.validate_test(node(meta=metadata), 100)


def test_legacy_capture_settings_are_allowed_for_full_capture():
    metadata = {"owner": "top", "dbt_dqm": {"granularity": ["id"], "owner": "nested",
                "capture_mode": "keys_only", "context_columns": ["value"]}}
    assert not runner.validate_test(node(meta=metadata), 100)


def test_request_is_a_file_instead_of_windows_command_line_payload(monkeypatch):
    selection = ["a test with spaces " + "x" * 50 for _ in range(10_000)]
    request = Namespace(command="run", select=selection, vars="{example: true}")
    calls = []

    def subprocess(command, **kwargs):
        import json

        calls.append(command)
        saved = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
        assert saved["select"] == selection
        assert kwargs == {"check": False}
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(runner.subprocess, "run", subprocess)
    assert runner.launch(request) == 1
    assert calls[0][0] == runner.sys.executable
    assert len(calls[0]) == 4


def test_run_cli_defaults_and_options():
    options = parser().parse_args(["run", "--select", "tag:business", "test_name",
                                        "--exclude", "tag:skip", "--threads", "4", "--full-refresh"])
    assert options.project_dir is options.profiles_dir is options.target is None
    assert options.select == ["tag:business", "test_name"] and options.threads == 4
    assert options.full_refresh


def test_workspace_keeps_epoch_on_edits_and_refuses_a_new_installation(tmp_path):
    workspace = Workspace(tmp_path / "workspace.sqlite")
    workspace.replace_snapshot([{"occurrence_id": "same-id", "notes": None, "_installation_id": "old"}])
    workspace.set_change("same-id", "notes", "reviewed")
    assert workspace.pending()[0].installation_id == "old"
    with pytest.raises(AppError, match="earlier DQM installation"):
        workspace.replace_snapshot([{"occurrence_id": "same-id", "notes": None, "_installation_id": "new"}])
    assert workspace.pending()[0].installation_id == "old"
