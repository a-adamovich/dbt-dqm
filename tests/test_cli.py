from dbt_dqm_app.cli import parser


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
