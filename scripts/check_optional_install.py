"""Smoke-test an installed wheel without importing the source checkout."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from tempfile import TemporaryDirectory

from dbt_dqm_app import errors, warehouse
from dbt_dqm_app.adapters import require_adapter
from dbt_dqm_app.config import AppConfig


def installed(name: str) -> bool:
    try:
        version(name)
    except PackageNotFoundError:
        return False
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("extras", choices=["base", "bigquery", "postgres", "both"])
    args = parser.parse_args()
    selected = {"bigquery", "postgres"} if args.extras == "both" else {args.extras}
    for adapter, driver in [("bigquery", "google-cloud-bigquery"),
                            ("postgres", "psycopg2-binary")]:
        assert not installed(f"dbt-{adapter}")
        assert installed(driver) == (adapter in selected)
    assert not installed("dbt-core") and not installed("dbt")
    for name in ("oauthlib",):
        assert installed(name) == ("bigquery" in selected), name
    import dbt_dqm_app.streamlit_app  # noqa: F401

    assert "psycopg2" not in sys.modules
    assert "google.cloud.bigquery" not in sys.modules
    for adapter in selected & {"bigquery", "postgres"}:
        if adapter == "postgres":
            driver_errors = import_module("psycopg2.errors")
            busy = driver_errors.LockNotAvailable("lock timeout")
            missing = driver_errors.UndefinedTable("missing health view")
        else:
            driver_errors = import_module("google.api_core.exceptions")
            busy = driver_errors.Conflict("concurrent update")
            missing = driver_errors.NotFound("missing health view")
        assert isinstance(errors.classify(busy), errors.WarehouseBusy)
        original = warehouse._query_rows

        def query_rows(config, query, missing=missing):
            if "dqm_maintenance_health" in query:
                raise missing
            return []

        warehouse._query_rows = query_rows
        try:
            settings = AppConfig(Path("."), Path("."), "dev", "smoke", adapter, "db",
                                 "schema", "US", "oauth", None)
            assert warehouse.fetch_health(settings)["maintenance"] is None
        finally:
            warehouse._query_rows = original

    assert "dbt" not in sys.modules
    with TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "dbt_project.yml").write_text("name: smoke\nprofile: smoke\n")
        cli = shutil.which("dbt-dqm", path=str(Path(sys.executable).parent))
        assert cli is not None
        subprocess.run([cli, "run", "--help"], check=True, capture_output=True, text=True)
        subprocess.run([cli, "--help"], check=True, capture_output=True)
        subprocess.run([cli, "workspace", "info", "--project-dir", directory,
                        "--target", "smoke"], check=True, capture_output=True)
        for adapter in ("bigquery", "postgres"):
            if adapter in selected:
                require_adapter(adapter)
                continue
            config = AppConfig(root, root, "dev", "smoke", adapter, "db", "schema", "US",
                               "oauth", None)
            connect = warehouse.client_for if adapter == "bigquery" else warehouse._postgres_connection
            for operation in (require_adapter, warehouse.validate_dbt, connect):
                try:
                    operation(adapter if operation is require_adapter else config)
                except errors.AppError as error:
                    assert f"dbt-dqm[{adapter}]" in error.message
                else:
                    raise AssertionError(f"Missing {adapter} did not produce an installation hint")
    print(f"Verified {args.extras} wheel installation")


if __name__ == "__main__":
    main()
