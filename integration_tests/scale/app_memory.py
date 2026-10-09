"""Measure what one review-app sync costs a fresh process: wall time and peak RSS.

    uv run python integration_tests/scale/app_memory.py \\
      --project-dir PATH --profiles-dir PATH --target dev

It repeats the app's sync path outside Streamlit: the verified issue and Health reads, replacing
the SQLite snapshot, reading the cache back, and building the issue DataFrame with each card's
record display, as the Issues tab does on every rerun. Peak RSS covers the whole process (Python
objects, pandas/numpy buffers and the database client), unlike tracemalloc. A running Streamlit
server adds its own baseline (measure it with `ps -o rss` on the app process); this probe
reports the data-dependent part plus the interpreter and libraries.
"""

from __future__ import annotations

import argparse
import json
import resource
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd

from dbt_dqm_app.config import load_config
from dbt_dqm_app.display import paired_display
from dbt_dqm_app.store import Workspace
from dbt_dqm_app.warehouse import fetch_health, fetch_issues


def peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # ru_maxrss is bytes on macOS and kilobytes on Linux.
    return round(peak / 2**20 if sys.platform == "darwin" else peak / 2**10, 1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--profiles-dir", required=True)
    parser.add_argument("--target", default="dev")
    args = parser.parse_args()
    baseline = peak_rss_mb()
    config = load_config(args.project_dir, args.profiles_dir, args.target)
    started = time.perf_counter()
    issues = fetch_issues(config)
    health = fetch_health(config)
    fetched = time.perf_counter()
    with tempfile.TemporaryDirectory() as directory:
        workspace = Workspace(Path(directory) / "workspace.sqlite")
        workspace.replace_synced_data(issues, health, max_issues=config.max_cache_issues,
                                      max_bytes=config.max_cache_bytes)
        del issues
        rows = workspace.rows()
        frame = pd.DataFrame(rows)
        frame["record_details"] = frame["record_values_json"].map(paired_display)
        payload_bytes = int(frame["record_values_json"].fillna("").str.len().mean() or 0)
    finished = time.perf_counter()
    print(
        json.dumps(
            {
                "issues": len(frame),
                "mean_payload_bytes": payload_bytes,
                "warehouse_read_seconds": round(fetched - started, 2),
                "total_seconds": round(finished - started, 2),
                "baseline_rss_mb": baseline,
                "peak_rss_mb": peak_rss_mb(),
            }
        )
    )


if __name__ == "__main__":
    main()
