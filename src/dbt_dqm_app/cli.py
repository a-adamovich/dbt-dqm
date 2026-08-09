from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="dbt-dqm")
    commands = root.add_subparsers(dest="command", required=True)
    app = commands.add_parser("app", help="Launch the local review application")
    app.add_argument("--project-dir", required=True)
    app.add_argument("--profiles-dir", required=True)
    app.add_argument("--target", required=True)
    app.add_argument("--port", type=int, default=8501)
    return root


def main() -> None:
    args = parser().parse_args()
    if args.command == "app":
        environment = os.environ.copy()
        environment.update(
            {
                "DBT_DQM_PROJECT_DIR": str(Path(args.project_dir).expanduser().resolve()),
                "DBT_DQM_PROFILES_DIR": str(Path(args.profiles_dir).expanduser().resolve()),
                "DBT_DQM_TARGET": args.target,
                "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
            }
        )
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
