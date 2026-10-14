"""Configuration and isolation boundaries for the dedicated DQM command."""
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from dbt_dqm_app import runner
from dbt_dqm_app.cli import parser
from dbt_dqm_app.errors import AppError
from dbt_dqm_app.store import Workspace


@pytest.mark.parametrize("value", [None, "", "team", [], ["a", " b ", ""]])
@pytest.mark.parametrize("nested", [False, True])
def test_metadata_owner_accepts_lists_without_mutation(value, nested):
    meta = {"dbt_dqm": {"granularity": ["id"]}}
    (meta["dbt_dqm"] if nested else meta)["owner"] = value
    assert not runner.validate_test(node(meta=meta), 100)
    assert (meta["dbt_dqm"] if nested else meta)["owner"] == value


@pytest.mark.parametrize("value", [1, True, {}, ["team", 1], [None]])
@pytest.mark.parametrize("nested", [False, True])
def test_owner_error_identifies_effective_key_value_and_type(value, nested):
    meta = {"dbt_dqm": {"granularity": ["id"]}}
    (meta["dbt_dqm"] if nested else meta)["owner"] = value
    errors = runner.validate_test(node(meta=meta), 100)
    assert len(errors) == 1
    assert ("meta.dbt_dqm.owner" if nested else "meta.owner") in errors[0]
    assert repr(value) in errors[0] and type(value).__name__ in errors[0]


def workflow_factory(calls, responses=None):
    """Exercise output boundaries and callback processing without a warehouse."""
    responses = responses or {}

    def factory(**options):
        def invoke(arguments, **kwargs):
            calls.append(arguments)
            print('{"unique_id":"test.unrelated"}')
            print("Compiled node 'sample' is: select private_column from sample")
            print("Running with dbt=1.12.5")
            for message, level in [("DQM_SETTINGS={\"default_limit\":100}", "debug"),
                                   ("important warning", "warn"), ("important warning", "warn")]:
                event = SimpleNamespace(data=SimpleNamespace(msg=message),
                                        info=SimpleNamespace(msg=message, level=level))
                options["callbacks"][0](event)
            return responses.get(arguments[0], SimpleNamespace(success=True, result=[], exception=None))
        return SimpleNamespace(invoke=invoke)
    return factory


@pytest.mark.parametrize("verbose", [False, True])
def test_output_is_captured_without_losing_callbacks(verbose, capsys):
    workflow = runner.Workflow({"verbose": verbose}, factory=workflow_factory([]))
    workflow.checked(["ls"])
    output = capsys.readouterr()
    assert ("test.unrelated" in output.out) is verbose
    assert ("Compiled node" in output.out) is verbose
    assert ("Running with dbt" in output.out) is verbose
    assert workflow.settings == {"default_limit": 100}
    if not verbose:
        assert output.err.count("important warning") == 1
        assert "DQM_SETTINGS" not in output.out + output.err


def test_unsuccessful_result_without_exception_has_reason():
    failed = SimpleNamespace(success=False, exception=None,
                             result=SimpleNamespace(results=[SimpleNamespace(
                                 status="error", message="Missing upstream table")]))
    workflow = runner.Workflow({}, factory=workflow_factory([], {"compile": failed}))
    with pytest.raises(RuntimeError, match="Missing upstream table"):
        workflow.checked(["compile"])


def test_defer_flags_are_phase_specific_and_preserve_environment(monkeypatch):
    monkeypatch.setenv("DBT_DEFER", "true")
    workflow = runner.Workflow({}, factory=workflow_factory([]))
    assert "--defer" not in workflow.phase_flags("test")
    assert "--no-defer" not in workflow.phase_flags("compile")
    workflow = runner.Workflow({"state": "prod", "defer": False, "favor_state": True},
                               factory=workflow_factory([]))
    for phase in ("ls", "compile", "test"):
        assert "--state" in workflow.phase_flags(phase)
    for phase in ("parse", "run-operation", "run"):
        assert "--no-defer" in workflow.phase_flags(phase)
        assert "--no-favor-state" in workflow.phase_flags(phase)
        assert "--state" not in workflow.phase_flags(phase)
    assert "--no-defer" in workflow.phase_flags("test")
    assert "--favor-state" in workflow.phase_flags("compile")
    assert "--cache-selected-only" in workflow.phase_flags("run")


@pytest.mark.parametrize("source", ["cli", "DBT_STATE", "DBT_DEFER_STATE"])
def test_state_collision_stops_before_invocation_and_artifact_writes(tmp_path, monkeypatch, source):
    monkeypatch.delenv("DBT_STATE", raising=False)
    monkeypatch.delenv("DBT_DEFER_STATE", raising=False)
    config = SimpleNamespace(project_dir=tmp_path, target_path=tmp_path / "saved state")
    monkeypatch.setattr(runner, "load_config", lambda *args, **kwargs: config)
    calls = []
    request = {"state": "saved state"} if source == "cli" else {}
    if source != "cli":
        monkeypatch.setenv(source, "saved state")
    workflow = runner.Workflow(request, factory=workflow_factory(calls))
    with pytest.raises(ValueError, match="equals target-path"):
        workflow.execute()
    assert calls == [] and not config.target_path.exists()


def test_metrics_survive_failure_and_resolved_artifact_path_is_shared(tmp_path, monkeypatch):
    import json

    config = SimpleNamespace(project_dir=tmp_path, target_path=tmp_path / "custom artifacts")
    monkeypatch.setattr(runner, "load_config", lambda *args, **kwargs: config)
    monkeypatch.delenv("DBT_STATE", raising=False)
    monkeypatch.delenv("DBT_DEFER_STATE", raising=False)
    calls = []
    workflow = runner.Workflow({"state": "prod artifacts"}, factory=workflow_factory(calls))

    def execute(config):
        workflow.invoke(["compile"])
        raise ValueError("invalid output")

    monkeypatch.setattr(workflow, "_execute", execute)
    with pytest.raises(ValueError, match="invalid output"):
        workflow.execute()
    metrics = json.loads((config.target_path / "dqm_workflow_metrics.json").read_text())
    assert metrics["completion_status"] == "error" and metrics["exit_code"] == 2
    assert metrics["phase_seconds"]["compile"] >= 0
    assert calls[0][calls[0].index("--target-path") + 1] == str(config.target_path)
    assert calls[0][calls[0].index("--state") + 1] == str(tmp_path / "prod artifacts")


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
