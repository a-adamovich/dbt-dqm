"""Exercise the engine boundary without dbt Python objects or a live warehouse."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from packaging.version import Version

from dbt_dqm_app import dbt_cli
from dbt_dqm_app.config import load_config
from dbt_dqm_app.errors import AppError


@pytest.fixture
def engine(monkeypatch, tmp_path):
    monkeypatch.setattr(dbt_cli, "resolve_dbt_executable", lambda value=None: "/selected/dbt")
    monkeypatch.setattr(dbt_cli, "engine_version", lambda path: Version("2.0.9"))
    cli = dbt_cli.DbtCLI()
    return cli, tmp_path / "artifacts"


def write_artifacts(target, *, status="pass", invocation="fresh", manifest_invocation=None):
    target.mkdir(parents=True, exist_ok=True)
    node = {"unique_id": "test.demo.check", "name": "check", "config": {"meta": {"dbt_dqm": {}}}}
    (target / "manifest.json").write_text(json.dumps({
        "metadata": {"invocation_id": manifest_invocation or invocation},
        "nodes": {node["unique_id"]: node}, "sources": {},
    }))
    (target / "run_results.json").write_text(json.dumps({
        "metadata": {"invocation_id": invocation},
        "results": [{"unique_id": node["unique_id"], "status": status, "failures": 0,
                     "message": None, "execution_time": 0.1}],
    }))


@pytest.mark.parametrize("code,status", [(0, "pass"), (1, "fail"), (1, "error")])
def test_results_are_snapshotted_and_linked_to_their_manifest(engine, monkeypatch, code, status):
    cli, target = engine
    def run(command, **kwargs):
        write_artifacts(target, status=status)
        return subprocess.CompletedProcess(command, code, "", "")
    monkeypatch.setattr(dbt_cli.subprocess, "run", run)
    result = cli.invoke(["test", "--target-path", str(target)])
    assert result.success is (code == 0)
    assert result.result.results[0].status == status
    assert result.result.results[0].node.name == "check"
    (target / "run_results.json").write_text("overwritten by a later phase")
    result.result.write(target / "dqm_run_results.json")
    assert json.loads((target / "dqm_run_results.json").read_text())["metadata"]["invocation_id"] == "fresh"


@pytest.mark.parametrize("code", [0, 1, 2])
def test_missing_fresh_artifact_never_reuses_previous_results(engine, monkeypatch, code):
    cli, target = engine
    write_artifacts(target, invocation="stale")
    monkeypatch.setattr(dbt_cli.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, code, "failure detail", "stderr detail"))
    result = cli.invoke(["test", "--target-path", str(target)])
    assert not result.success and result.result is None
    assert result.exception is not None
    assert not (target / "run_results.json").exists()


def test_mismatched_invocations_are_rejected(engine, monkeypatch):
    cli, target = engine
    def run(command, **kwargs):
        write_artifacts(target, manifest_invocation="another run")
        return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(dbt_cli.subprocess, "run", run)
    result = cli.invoke(["test", "--target-path", str(target)])
    assert not result.success and "different invocations" in str(result.exception)


def test_events_lists_and_dqm_messages_are_separate(engine, monkeypatch):
    cli, target = engine
    events = []
    cli.callbacks = [events.append]
    marker = 'DQM_SETTINGS={"default_limit":100}'
    output = "\n".join([json.dumps({"data": {}, "info": {"msg": marker, "level": "info"}}),
                        json.dumps({"unique_id": "test.demo.check", "fqn": ["demo", "check"]}),
                        json.dumps({"data": {"msg": json.dumps({"unique_id": "test.demo.other"})},
                                    "info": {"name": "PrintEvent", "level": "info"}}),
                        "non-JSON diagnostic"])
    monkeypatch.setattr(dbt_cli.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 0, output, ""))
    result = cli.invoke(["ls", "--output", "json", "--target-path", str(target)])
    assert len(result.result) == 2
    assert events[0].data.msg == marker


def test_v2_forces_fresh_execution_and_json_without_changing_parent_environment(engine, monkeypatch):
    cli, target = engine
    monkeypatch.setenv("DBT_WRITE_JSON", "false")
    monkeypatch.setenv("DBT_ENGINE_BATCH_TESTS", "true")
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        write_artifacts(target)
        return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(dbt_cli.subprocess, "run", run)
    cli.invoke(["test", "--target-path", str(target), "--vars", "{a: original}"], vars={"a": "override"})
    command, options = calls[0]
    assert command[0] == "/selected/dbt"
    assert "--no-manage-state" in command and "--write-json" in command
    assert command.count("--vars") == 1
    assert yaml.safe_load(command[command.index("--vars") + 1]) == {"a": "override"}
    assert options["env"]["DBT_WRITE_JSON"] == "true"
    assert options["env"]["DBT_ENGINE_BATCH_TESTS"] == "false"
    assert dbt_cli.os.environ["DBT_WRITE_JSON"] == "false"


def test_test_projection_keeps_artifact_state_and_log_paths_at_consumer(engine, monkeypatch, tmp_path):
    cli, _ = engine
    project = tmp_path / "consumer with spaces"
    project.mkdir()
    (project / "dbt_project.yml").write_text("name: consumer\nprofile: consumer\nlog-path: custom-logs\n")
    target = project / "artifacts"
    monkeypatch.setenv("DBT_STATE", "saved-state")
    seen = []

    def run(command, **kwargs):
        seen.append((command, kwargs))
        projected = Path(command[command.index("--project-dir") + 1])
        assert projected != project and projected.exists()
        assert Path(command[command.index("--target-path") + 1]) == target
        assert Path(command[command.index("--log-path") + 1]) == project / "custom-logs"
        assert kwargs["env"]["DBT_STATE"] == str(project / "saved-state")
        write_artifacts(target)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(dbt_cli.subprocess, "run", run)
    result = cli.invoke(["test", "--project-dir", str(project), "--target-path", str(target),
                         "--vars", "{_dbt_dqm_workflow: true}"])
    assert result.success, result.exception
    assert len(seen) == 1
    assert not Path(seen[0][0][seen[0][0].index("--project-dir") + 1]).exists()


@pytest.mark.parametrize("version,adapter,valid", [
    ("1.11.0", "postgres", True), ("1.12.5", "bigquery", True),
    ("2.0.9", "bigquery", True), ("2.0.9", "postgres", False),
    ("1.10.0", "bigquery", False), ("2.0.8", "bigquery", False),
    ("2.1.0a1", "bigquery", False), ("3.0.0", "bigquery", False),
])
def test_supported_engines_and_warehouses(engine, monkeypatch, version, adapter, valid):
    cli, _ = engine
    monkeypatch.setattr(dbt_cli, "engine_version", lambda path: Version(version))
    if valid:
        cli.validate(adapter)
    else:
        with pytest.raises(AppError):
            cli.validate(adapter)


def test_explicit_executable_precedes_environment_and_does_not_silently_fall_back(monkeypatch):
    monkeypatch.setenv("DBT_DQM_DBT_EXECUTABLE", "/environment/dbt")
    monkeypatch.setattr(dbt_cli.shutil, "which", lambda name, **kwargs: name if name != "/missing/dbt" else None)
    assert dbt_cli.resolve_dbt_executable("/explicit/dbt") == str(Path("/explicit/dbt").resolve())
    assert dbt_cli.resolve_dbt_executable() == str(Path("/environment/dbt").resolve())
    with pytest.raises(AppError, match="selected dbt executable"):
        dbt_cli.resolve_dbt_executable("/missing/dbt")


@pytest.mark.parametrize("output", ["Core:\n  - installed: 1.12.5\nPlugins:\n - bigquery: 1.12.1",
                                   "dbt 2.0.9\n"])
def test_version_detection_uses_engine_not_adapter_version(monkeypatch, output):
    dbt_cli.engine_version.cache_clear()
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, output, "")
    monkeypatch.setattr(dbt_cli.subprocess, "run", run)
    try:
        version = dbt_cli.engine_version("/version-test/dbt")
        assert version == Version("2.0.9" if output.startswith("dbt") else "1.12.5")
        assert dbt_cli.engine_version("/version-test/dbt") == version and len(calls) == 1
    finally:
        dbt_cli.engine_version.cache_clear()


def test_profile_errors_do_not_disclose_rendered_credentials(tmp_path, monkeypatch):
    (tmp_path / "dbt_project.yml").write_text("name: demo\nprofile: demo\n")
    (tmp_path / "profiles.yml").write_text("demo:\n  target: dev\n  outputs:\n    dev:\n      password: \"{{ env_var('DBT_ENV_SECRET_TEST') }}{{ missing }}\"\n")
    monkeypatch.setenv("DBT_ENV_SECRET_TEST", "private-value")
    with pytest.raises(AppError) as caught:
        load_config(tmp_path, tmp_path)
    assert "private-value" not in str(caught.value) + caught.value.details
