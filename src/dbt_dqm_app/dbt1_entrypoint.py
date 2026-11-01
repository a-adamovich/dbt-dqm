"""CLI entrypoint executed by the selected v1 engine's Python, never the app's Python.

dbt-bigquery 1.11/1.12 can open an unused lazy handle while closing it after offline
compilation. Avoid that accidental authentication/network access. Open handles still use
the adapter's normal cleanup. This shim neither injects manifests nor registers callbacks.
"""

import sys
from pathlib import Path

# Executing a file adds its directory to sys.path. dbt v1 scans top-level dbt_* modules
# as plugins, so our sibling dbt_cli.py must not become an apparent top-level plugin.
sys.path = [path for path in sys.path if Path(path).resolve() != Path(__file__).parent.resolve()]


def main():
    from dbt.cli.main import cli

    try:
        from dbt.adapters.bigquery.connections import BigQueryConnectionManager
        from dbt.adapters.contracts.connection import ConnectionState
    except ModuleNotFoundError as error:
        if error.name != "dbt.adapters.bigquery":
            raise
    else:
        original = BigQueryConnectionManager.close.__func__

        def close_unused(cls, connection):
            if connection.state != ConnectionState.OPEN:
                connection.handle = None
                connection.state = ConnectionState.CLOSED
                return connection
            return original(cls, connection)

        BigQueryConnectionManager.close = classmethod(close_unused)
    return cli()


if __name__ == "__main__":
    raise SystemExit(main())
