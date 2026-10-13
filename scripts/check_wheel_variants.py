"""Install all adapter variants in native temporary environments on any OS."""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    uv = shutil.which("uv")
    assert uv, "uv must be available"
    smoke = Path(__file__).with_name("check_optional_install.py").resolve()
    with TemporaryDirectory(prefix="dbt-dqm-wheel-") as directory:
        for variant, extras in [("base", ""), ("bigquery", "[bigquery]"),
                                ("postgres", "[postgres]"), ("both", "[bigquery,postgres]")]:
            environment = Path(directory) / variant
            subprocess.run([uv, "venv", str(environment), "--python", sys.executable], check=True)
            python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
            subprocess.run([uv, "pip", "install", "--python", str(python), str(wheel) + extras], check=True)
            subprocess.run([uv, "pip", "check", "--python", str(python)], check=True)
            subprocess.run([str(python), str(smoke), variant], check=True)


if __name__ == "__main__":
    main()
