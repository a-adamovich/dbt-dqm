from dbt_dqm_app.cli import parser


def test_run_flags_and_boolean_defaults():
    options = parser().parse_args(["run"])
    assert options.defer is options.favor_state is options.state is options.target_path is None
    assert options.verbose is False
    options = parser().parse_args(["run", "--full-refresh", "--defer", "--favor-state",
                                   "--state", "prod state", "--target-path", "dev output", "--verbose"])
    assert options.full_refresh and options.defer and options.favor_state and options.verbose
    assert options.state == "prod state" and options.target_path == "dev output"
    options = parser().parse_args(["run", "--no-defer", "--no-favor-state"])
    assert options.defer is False and options.favor_state is False


def test_unknown_arguments_keep_original_dashes(capsys):
    import pytest

    with pytest.raises(SystemExit):
        parser().parse_args(["run", "--full-refresh", "--unknown-option"])
    error = capsys.readouterr().err
    assert "unrecognized arguments: --unknown-option" in error
    assert "unrecognized arguments: full-refresh" not in error


def test_app_custom_artifacts_override_environment(monkeypatch):
    from unittest.mock import MagicMock

    import pytest

    from dbt_dqm_app import cli

    monkeypatch.setenv("DBT_TARGET_PATH", "old")
    process = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(cli.subprocess, "run", process)
    monkeypatch.setattr(cli.sys, "argv", ["dbt-dqm", "app", "--target-path", "custom artifacts"])
    with pytest.raises(SystemExit):
        cli.main()
    assert process.call_args.kwargs["env"]["DBT_TARGET_PATH"] == "custom artifacts"


def test_cache_flags_are_passed_to_the_app_environment(monkeypatch):
    from unittest.mock import MagicMock

    import pytest

    from dbt_dqm_app import cli

    process = MagicMock()
    process.return_value.returncode = 0
    monkeypatch.setattr(cli.subprocess, "run", process)
    monkeypatch.setattr(cli.sys, "argv", ["dbt-dqm", "app", "--project-dir", "/tmp/project",
                                         "--profiles-dir", "/tmp/profiles", "--target", "dev",
                                         "--max-cache-issues", "100", "--max-cache-mib", "2",
                                         "--max-search-gib", "0.5", "--browse", "auto"])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 0
    assert process.call_args.kwargs["env"]["DBT_DQM_MAX_CACHE_ISSUES"] == "100"
    assert process.call_args.kwargs["env"]["DBT_DQM_MAX_CACHE_MIB"] == "2"
    assert process.call_args.kwargs["env"]["DBT_DQM_MAX_SEARCH_GIB"] == "0.5"
    assert process.call_args.kwargs["env"]["DBT_DQM_BROWSE"] == "auto"


def test_cli_rejects_limits_above_the_supported_ceiling(monkeypatch):
    import pytest

    from dbt_dqm_app import cli

    monkeypatch.setattr(cli.sys, "argv", ["dbt-dqm", "app", "--project-dir", "/tmp/project",
                                         "--profiles-dir", "/tmp/profiles", "--target", "dev",
                                         "--max-cache-issues", "50001"])
    with pytest.raises(SystemExit, match="1 to 50000"):
        cli.main()
    monkeypatch.setattr(cli.sys, "argv", ["dbt-dqm", "app", "--project-dir", "/tmp/project",
                                         "--profiles-dir", "/tmp/profiles", "--target", "dev",
                                         "--max-search-gib", "0"])
    with pytest.raises(SystemExit, match="max-search-gib"):
        cli.main()


def test_workspace_info_cli_parses_without_profiles_directory():
    args = parser().parse_args(
        ["workspace", "info", "--project-dir", "/tmp/project", "--target", "dev"]
    )
    assert args.workspace_command == "info"
    assert args.target == "dev"


def test_workspace_purge_cli_parses():
    args = parser().parse_args(
        ["workspace", "purge", "--project-dir", "/tmp/project", "--target", "prod"]
    )
    assert args.workspace_command == "purge"


def test_engine_override_is_available_for_all_commands():
    for command in (["run"], ["app"], ["workspace", "info"], ["workspace", "purge"]):
        args = parser().parse_args([*command, "--dbt-executable", "/path with spaces/dbt"])
        assert args.dbt_executable == "/path with spaces/dbt"


def test_app_passes_explicit_engine_to_streamlit(monkeypatch):
    from unittest.mock import MagicMock

    import pytest

    from dbt_dqm_app import cli

    process = MagicMock()
    process.return_value.returncode = 0
    monkeypatch.setattr(cli.subprocess, "run", process)
    monkeypatch.setattr(cli.sys, "argv", ["dbt-dqm", "app", "--dbt-executable", "/engine/dbt"])
    monkeypatch.setenv("DBT_DQM_DBT_EXECUTABLE", "/older/dbt")
    with pytest.raises(SystemExit):
        cli.main()
    assert process.call_args.kwargs["env"]["DBT_DQM_DBT_EXECUTABLE"] == "/engine/dbt"
