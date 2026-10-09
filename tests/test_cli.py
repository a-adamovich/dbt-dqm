from dbt_dqm_app.cli import parser


def test_cache_flags_are_passed_to_the_app_environment(monkeypatch):
    from unittest.mock import MagicMock

    import pytest

    from dbt_dqm_app import cli

    process = MagicMock()
    process.return_value.returncode = 0
    monkeypatch.setattr(cli.subprocess, "run", process)
    monkeypatch.setattr(cli.sys, "argv", ["dbt-dqm", "app", "--project-dir", "/tmp/project",
                                         "--profiles-dir", "/tmp/profiles", "--target", "dev",
                                         "--max-cache-issues", "100", "--max-cache-mib", "2"])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 0
    assert process.call_args.kwargs["env"]["DBT_DQM_MAX_CACHE_ISSUES"] == "100"
    assert process.call_args.kwargs["env"]["DBT_DQM_MAX_CACHE_MIB"] == "2"


def test_cli_rejects_limits_above_the_supported_ceiling(monkeypatch):
    import pytest

    from dbt_dqm_app import cli

    monkeypatch.setattr(cli.sys, "argv", ["dbt-dqm", "app", "--project-dir", "/tmp/project",
                                         "--profiles-dir", "/tmp/profiles", "--target", "dev",
                                         "--max-cache-issues", "50001"])
    with pytest.raises(SystemExit, match="1 to 50000"):
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
