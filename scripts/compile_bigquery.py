"""Compile offline, working around dbt-bigquery 1.11 opening idle handles on close.

This affects only this compile process. Any SQL execution or introspection is rejected,
so a credential-free check cannot silently become a warehouse operation.
"""

from __future__ import annotations

import sys
from pathlib import Path

from dbt.adapters.bigquery.connections import BigQueryConnectionManager
from dbt.adapters.contracts.connection import ConnectionState
from dbt.cli.main import dbtRunner


def closed_without_opening(cls, connection):
    connection.state = ConnectionState.CLOSED
    return connection


def reject_warehouse_access(*args, **kwargs):
    raise RuntimeError("Offline BigQuery compilation attempted warehouse access")


if __name__ == "__main__":
    BigQueryConnectionManager.close = classmethod(closed_without_opening)
    BigQueryConnectionManager.open = classmethod(reject_warehouse_access)
    project = Path(__file__).resolve().parents[1] / "integration_tests/demo_bigquery"
    result = dbtRunner().invoke(
        [
            "--no-populate-cache",
            "compile",
            "--no-introspect",
            "--project-dir",
            str(project),
            *sys.argv[1:],
        ]
    )
    raise SystemExit(0 if result.success else 1)
