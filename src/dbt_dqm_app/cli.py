from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from dbt_dqm_app.config import resolve_workspace_path
from dbt_dqm_app.limits import MAX_CACHE_ISSUES, MAX_CACHE_MIB, validate_limits


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="dbt-dqm")
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run the separate DQM test, capture and review workflow")
    for flag in ("project-dir", "profiles-dir", "target"):
        run.add_argument(f"--{flag}")
    run.add_argument("--select", nargs="+")
    run.add_argument("--exclude", nargs="+")
    run.add_argument("--vars", default="{}")
    run.add_argument("--threads", type=int)
    run.add_argument("--full-refresh", action="store_true",
                     help="Reset DQM history and annotations; preserve unrelated relations")
    app = commands.add_parser("app", help="Launch the local review application")
    app.add_argument("--project-dir", default=None)
    app.add_argument("--profiles-dir", default=None)
    app.add_argument("--target", default=None)
    app.add_argument("--port", type=int, default=8501)
    app.add_argument("--max-cache-issues", type=int, default=MAX_CACHE_ISSUES,
                     help="Issue cache ceiling, configurable downward from 50000")
    app.add_argument("--max-cache-mib", type=int, default=MAX_CACHE_MIB,
                     help="Serialized issue cache ceiling, configurable downward from 128 MiB")
    app.add_argument("--max-search-gib", type=float, default=1.0,
                     help="BigQuery bytes one search may process in warehouse browsing (GiB)")
    app.add_argument("--browse", choices=("snapshot", "warehouse", "auto"), default="auto",
                     help="auto (default): sync all issues locally while they fit the cache "
                     "limits, read pages from the warehouse above them; snapshot or warehouse: "
                     "always that mode")
    app.add_argument(
        "--archive-cache-days",
        type=int,
        default=90,
        help="Archived history retained in SQLite; zero disables archived caching",
    )
    app.add_argument(
        "--lock-timeout",
        type=int,
        default=5,
        help="Seconds a Postgres write waits for a lock before the app reports the warehouse busy",
    )
    workspace = commands.add_parser("workspace", help="Inspect or purge local failed-row data")
    workspace_commands = workspace.add_subparsers(dest="workspace_command", required=True)
    for name, help_text in (
        ("info", "Show the local workspace location and size"),
        ("purge", "Permanently delete the local snapshot and pending patches"),
    ):
        command = workspace_commands.add_parser(name, help=help_text)
        command.add_argument("--project-dir")
        command.add_argument("--target")
        command.add_argument("--profiles-dir")
    return root


def main() -> None:
    args = parser().parse_args()
    if args.command == "run":
        from dbt_dqm_app.runner import launch

        raise SystemExit(launch(args))
    if args.command == "workspace":
        workspace_path = resolve_workspace_path(args.project_dir, args.target, args.profiles_dir)
        related_paths = [
            workspace_path,
            Path(f"{workspace_path}-wal"),
            Path(f"{workspace_path}-shm"),
        ]
        if args.workspace_command == "info":
            size = sum(path.stat().st_size for path in related_paths if path.exists())
            print(f"Workspace: {workspace_path}")
            print(f"Exists: {'yes' if workspace_path.exists() else 'no'}")
            print(f"Size: {size} bytes")
            return
        for path in related_paths:
            if path.exists():
                path.unlink()
        try:
            workspace_path.parent.rmdir()
        except OSError:
            pass
        print(f"Purged workspace: {workspace_path}")
        return
    if args.command == "app":
        try:
            validate_limits(args.max_cache_issues, args.max_cache_mib)
        except ValueError as error:
            raise SystemExit(str(error)) from error
        if not 0 < args.max_search_gib <= 1024:
            raise SystemExit("--max-search-gib must be greater than 0 and at most 1024")
        if args.archive_cache_days < 0:
            raise SystemExit("--archive-cache-days must be zero or a positive integer")
        if args.lock_timeout <= 0:
            raise SystemExit("--lock-timeout must be a positive integer")
        environment = os.environ.copy()
        environment.update(
            {
                "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
                "DBT_DQM_ARCHIVE_CACHE_DAYS": str(args.archive_cache_days),
                "DBT_DQM_LOCK_TIMEOUT_SECONDS": str(args.lock_timeout),
                "DBT_DQM_MAX_CACHE_ISSUES": str(args.max_cache_issues),
                "DBT_DQM_MAX_CACHE_MIB": str(args.max_cache_mib),
                "DBT_DQM_MAX_SEARCH_GIB": str(args.max_search_gib),
                "DBT_DQM_BROWSE": args.browse,
            }
        )
        for key, value in (("DBT_DQM_PROJECT_DIR", args.project_dir),
                           ("DBT_DQM_PROFILES_DIR", args.profiles_dir),
                           ("DBT_DQM_TARGET", args.target)):
            environment.pop(key, None)
            if value is not None:
                environment[key] = (value if key.endswith("TARGET")
                                    else str(Path(value).expanduser().resolve()))
        app_path = Path(__file__).with_name("streamlit_app.py")
        command = [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(app_path),
            "--server.port",
            str(args.port),
            "--server.headless",
            "true",
            "--server.address",
            "127.0.0.1",
        ]
        try:
            return_code = subprocess.run(command, env=environment, check=False).returncode
        except KeyboardInterrupt:
            return_code = 130
        raise SystemExit(return_code)


if __name__ == "__main__":
    main()
