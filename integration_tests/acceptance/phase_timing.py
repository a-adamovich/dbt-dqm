"""Optional per-phase timing for development measurements of the BigQuery suite."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

ENVIRONMENT = "DBT_DQM_BIGQUERY_TIMING_DIRECTORY"
RUNNER_FIELDS = ("phase_seconds", "sql_calls", "query_jobs", "total_seconds", "completion_status", "exit_code")


def record(phase, detail, seconds, **extra):
    """Append one timing record when a measurement directory is configured; otherwise do nothing."""
    directory = os.environ.get(ENVIRONMENT)
    if not directory:
        return
    test, _, stage = os.environ.get("PYTEST_CURRENT_TEST", "").rpartition(" (")
    row = {"test": test.rpartition("::")[2], "stage": stage.rstrip(")"), "phase": phase,
           "detail": detail, "seconds": round(seconds, 3), **extra}
    with (Path(directory) / f"{os.getpid()}.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps(row) + "\n")


def _modified(path):
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _runner_metrics(path, previous):
    """Read the runner's metrics only if this invocation wrote them; a stale file is not its data."""
    if path is None or _modified(path) in (None, previous):
        return None
    try:
        metrics = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {key: metrics.get(key) for key in RUNNER_FIELDS}


@contextmanager
def timed(phase, detail="", runner_metrics=None):
    """Time a block. With `runner_metrics`, attach that artifact's phase and query metrics."""
    if not os.environ.get(ENVIRONMENT):
        yield
        return
    previous = None if runner_metrics is None else _modified(runner_metrics)
    started = time.monotonic()
    try:
        yield
    finally:
        extra = {} if runner_metrics is None else {"runner": _runner_metrics(runner_metrics, previous)}
        record(phase, detail, time.monotonic() - started, **extra)
