"""Native CLI defaults, process boundaries and diagnostics on every supported OS."""

import os
import sys
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from google.api_core.exceptions import Forbidden, NotFound
from google.api_core.retry import if_exception_type

from dbt_dqm_app import cli, warehouse
from dbt_dqm_app.config import AppConfig, load_config, resolve_workspace_path
from dbt_dqm_app.errors import AppError, WarehouseBusy, classify
from dbt_dqm_app.limits import CacheLimitExceeded


def unsafe_failure():
    raise NotFound("dqm_issue_occurrences was not found",
                   response={"retry": if_exception_type(NotFound)})


def cache_failure():
    raise CacheLimitExceeded("too many issues", "cache detail")


def success():
    return 42


@pytest.fixture
def project(tmp_path, monkeypatch):
    for name in ("DBT_PROJECT_DIR", "DBT_PROFILES_DIR", "DBT_TARGET", "DBT_PROFILE", "DBT_TARGET_PATH"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "project with spaces"
    root.mkdir()
    (root / "dbt_project.yml").write_text("name: consumer\nprofile: consumer\n", encoding="utf-8")
    (root / "profiles.yml").write_text("""
consumer:
  target: "{{ env_var('DQM_TEST_TARGET', 'dev') }}"
  outputs:
    dev:
      type: postgres
      host: localhost
      user: reviewer
      password: "{{ env_var('DQM_TEST_SECRET', 'secret') }}"
      dbname: analytics
      schema: quality
    prod:
      type: postgres
      host: production
      user: reviewer
      password: secret
      dbname: analytics
      schema: quality
    unused:
      password: "{{ env_var('UNSET_OTHER_TARGET') }}"
""", encoding="utf-8")
    monkeypatch.chdir(root)
    return root


def test_no_flag_launch_and_selected_profile_rendering(project):
    config = load_config()
    assert config.project_dir == project
    assert config.profiles_dir == project
    assert config.target == "dev"
    assert config.dbt_cli_args == ()
    assert resolve_workspace_path() == config.workspace_path
    assert cli.parser().parse_args(["app"]).project_dir is None
    assert cli.parser().parse_args(["workspace", "info"]).target is None


def test_environment_and_explicit_precedence(project, monkeypatch):
    monkeypatch.setenv("DBT_PROJECT_DIR", str(project))
    monkeypatch.setenv("DBT_PROFILES_DIR", str(project))
    monkeypatch.setenv("DBT_TARGET", "prod")
    assert load_config().host == "production"
    config = load_config(target="dev")
    assert config.host == "localhost"
    assert config.dbt_cli_args == ("--target", "dev")
    monkeypatch.setenv("DBT_TARGET_PATH", "custom artifacts")
    assert load_config().target_path == project / "custom artifacts"


def test_parent_project_and_launch_cwd_are_distinct(project, monkeypatch):
    subfolder = project / "models"
    subfolder.mkdir()
    monkeypatch.chdir(subfolder)
    monkeypatch.setenv("DBT_PROFILES_DIR", str(project))
    config = load_config()
    assert config.project_dir == project
    assert config.working_dir == subfolder
    assert not config.dbt_cli_args


def test_profile_environment_override(project, monkeypatch):
    text = (project / "profiles.yml").read_text().replace("consumer:", "alternate:", 1)
    (project / "profiles.yml").write_text(text, encoding="utf-8")
    monkeypatch.setenv("DBT_PROFILE", "alternate")
    monkeypatch.setenv("DQM_TEST_TARGET", "prod")
    assert load_config().profile_name == "alternate"
    assert load_config().target == "prod"


def test_workspace_explicit_target_needs_no_profile_file(project):
    (project / "profiles.yml").unlink()
    assert resolve_workspace_path(target="dev").name == "workspace.sqlite"


def test_home_profile_fallback_and_tilde(project, monkeypatch):
    home = project / "home"
    profiles = home / ".dbt"
    profiles.mkdir(parents=True)
    (project / "profiles.yml").rename(profiles / "profiles.yml")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    from dbt.cli import resolvers
    monkeypatch.setattr(resolvers, "default_dbt_home_dir", lambda: profiles)
    assert load_config().profiles_dir == profiles


@pytest.mark.skipif(os.name != "nt", reason="Native Windows UNC path handling")
def test_windows_unc_path_parts_are_preserved(monkeypatch):
    from dbt_dqm_app.config import resolve_project_dir
    monkeypatch.setattr(Path, "resolve", lambda self: self)
    path = resolve_project_dir(r"\\server\share\project with spaces")
    assert path.drive == r"\\server\share"
    assert path.name == "project with spaces"


def test_cli_does_not_invent_overrides(project, monkeypatch):
    process = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(cli.subprocess, "run", process)
    monkeypatch.setattr(cli.sys, "argv", ["dbt-dqm", "app"])
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "existing ADC configuration")
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 0
    environment = process.call_args.kwargs["env"]
    assert "DBT_DQM_PROJECT_DIR" not in environment
    assert "DBT_DQM_TARGET" not in environment
    assert environment["GOOGLE_APPLICATION_CREDENTIALS"] == "existing ADC configuration"


def test_executable_search_prefers_interpreter_directory(tmp_path, monkeypatch):
    calls = []
    executable = tmp_path / "Scripts with spaces" / "dbt.exe"
    monkeypatch.setattr(warehouse.sys, "executable", str(executable.parent / "python.exe"))
    def which(name, path=None):
        calls.append(path)
        return str(executable)
    monkeypatch.setattr(warehouse.shutil, "which", which)
    assert warehouse.resolve_dbt_executable() == str(executable.resolve())
    assert calls == [str(Path(warehouse.sys.executable).parent)]


def test_executable_search_fallback_and_absence(tmp_path, monkeypatch):
    executable = tmp_path / "dbt"
    monkeypatch.setattr(warehouse.shutil, "which", lambda name, path=None:
                        None if path else str(executable))
    assert warehouse.resolve_dbt_executable() == str(executable.resolve())
    monkeypatch.setattr(warehouse.shutil, "which", lambda *args, **kwargs: None)
    with pytest.raises(AppError, match="could not be found"):
        warehouse.resolve_dbt_executable()


def test_real_environment_executable_is_selected():
    executable = Path(warehouse.resolve_dbt_executable())
    assert executable.parent == Path(warehouse.sys.executable).parent.resolve()


def test_validation_keeps_only_supplied_flags_and_adc(project, monkeypatch):
    config = load_config(target="dev")
    calls = []
    monkeypatch.setattr(warehouse, "_dbt_version", lambda executable: "dbt test version")
    monkeypatch.setattr(warehouse, "check_required_relations", lambda config: None)
    monkeypatch.setattr(warehouse.subprocess, "run", lambda command, **kwargs:
                        calls.append((command, kwargs)))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "existing ADC configuration")
    warehouse.validate_dbt(config)
    assert all(command[2:] == ["--target", "dev"] for command, _ in calls)
    assert all(kwargs["cwd"] == project for _, kwargs in calls)
    assert all(kwargs["env"]["GOOGLE_APPLICATION_CREDENTIALS"] == "existing ADC configuration"
               for _, kwargs in calls)


def test_subprocess_failure_keeps_both_streams_command_and_code():
    import subprocess
    error = subprocess.CalledProcessError(7, ["path with spaces/dbt.exe", "parse", "--target", "dev"],
                                          "stdout marker", "stderr marker")
    details = classify(error).details
    for value in ("path with spaces/dbt.exe", "--target", "Exit code: 7", "stdout marker", "stderr marker"):
        assert value in details


def test_spawn_pool_survives_unpicklable_errors_and_keeps_types(monkeypatch):
    # AppTest temporarily replaces __main__ with an unguarded generated script.
    monkeypatch.setattr(sys.modules["__main__"], "__file__", __file__)
    with ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn")) as executor:
        with pytest.raises(AppError, match="dqm_issue_occurrences was not found") as raised:
            executor.submit(warehouse.run_worker, unsafe_failure).result(timeout=30)
        assert "NotFound" in raised.value.details
        assert "pickle" not in raised.value.message
        with pytest.raises(CacheLimitExceeded) as raised:
            executor.submit(warehouse.run_worker, cache_failure).result(timeout=30)
        assert raised.value.details == "cache detail"
        assert executor.submit(warehouse.run_worker, success).result(timeout=30) == 42


def test_preflight_distinguishes_missing_relations_from_permissions(tmp_path, monkeypatch):
    config = AppConfig(tmp_path, tmp_path, "dev", "demo", "bigquery", "project", "schema", "US", "oauth", None)
    client = MagicMock()
    client.get_table.side_effect = NotFound("missing")
    monkeypatch.setattr(warehouse, "client_for", lambda config: client)
    with pytest.raises(AppError, match="dbt run --select package:dbt_dqm") as raised:
        warehouse.check_required_relations(config)
    assert "project.schema.dqm_issue_occurrences" in raised.value.details
    client.get_table.side_effect = Forbidden("no access")
    with pytest.raises(Forbidden):
        warehouse.check_required_relations(config)


def test_apply_refresh_error_is_serializable_after_commit(tmp_path, monkeypatch):
    import pickle
    config = AppConfig(tmp_path, tmp_path, "dev", "demo", "bigquery", "project", "schema", "US", "oauth", None)
    monkeypatch.setattr(warehouse, "validate_dbt", lambda config: None)
    monkeypatch.setattr(warehouse, "apply_patches", lambda *args: "committed")
    monkeypatch.setattr(warehouse, "fetch_issues", lambda config: unsafe_failure())
    batch, snapshot, error = pickle.loads(pickle.dumps(warehouse.apply_worker(config, [])))
    assert batch == "committed" and snapshot is None
    assert "dqm_issue_occurrences" in error.message
    assert isinstance(warehouse.worker_error(WarehouseBusy("busy")), WarehouseBusy)


def test_postgres_preflight_does_not_require_bigquery_staging(tmp_path, monkeypatch):
    config = AppConfig(tmp_path, tmp_path, "dev", "demo", "postgres", "db", "schema", "US", "oauth", None)
    connection = MagicMock()
    cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = ("present",)
    monkeypatch.setattr(warehouse, "_postgres_connection", lambda config: connection)
    warehouse.check_required_relations(config)
    assert all("dqm_app_change_staging" not in str(call) for call in cursor.execute.call_args_list)
    assert len(cursor.execute.call_args_list) == len(warehouse.REQUIRED_APP_RELATIONS)


def test_windows_current_directory_does_not_override_the_venv(tmp_path, monkeypatch):
    directory = tmp_path / "venv" / "Scripts"
    expected = directory / "dbt.exe"
    monkeypatch.setattr(warehouse.sys, "executable", str(directory / "python.exe"))
    monkeypatch.setattr(warehouse.sys, "platform", "win32")
    calls = []

    def which(command, path=None):
        calls.append((command, path))
        if command == "dbt":
            return str(tmp_path / "project" / "dbt.exe")
        if command == str(expected):
            return str(expected)
        return None

    monkeypatch.setattr(warehouse.shutil, "which", which)
    assert warehouse.resolve_dbt_executable() == str(expected)
    assert calls == [("dbt", str(directory)), (str(expected), None)]
