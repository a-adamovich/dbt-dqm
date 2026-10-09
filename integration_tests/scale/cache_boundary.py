"""Repeat the real Postgres sync memory probe at the supported 50,000-issue boundary.

Set DBT_DQM_TEST_DSN to a disposable database. Only a uniquely named schema is created/dropped.
The app_memory subprocess measures warehouse reads, atomic SQLite replacement and rendering.
"""

import argparse
import json
import os
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

from postgres_scale import app_sync, build_project, load_history


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    dsn = os.environ["DBT_DQM_TEST_DSN"]
    with tempfile.TemporaryDirectory(prefix="dqm-cache-boundary-") as directory:
        project = build_project(Path(directory), dsn, "dqm_cache_" + uuid.uuid4().hex[:12], 1)
        try:
            project.dbt("seed")
            project.dbt("run", "--select", "demo_records")
            project.dbt("test", "--select", "scale_t0000")
            project.dbt("run", "--select", "package:dbt_dqm")
            load_history(
                project,
                SimpleNamespace(
                    tests=1,
                    occurrences=50_000,
                    active_fraction=1.0,
                    executions=1,
                    observations=50_000,
                    payload_columns=8,
                ),
            )
            result = app_sync(project)
            assert result["issues"] == 50_000
            result["scenario"] = "supported-cache-boundary-real-postgres-sync"
            result["synthetic_processed_history"] = True
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result))
        finally:
            project.sql("drop schema if exists @schema cascade")
            project.sql(f'drop schema if exists "{project.schema}_dbt_test__audit" cascade')


if __name__ == "__main__":
    main()
