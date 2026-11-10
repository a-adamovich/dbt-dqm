"""Optional per-phase timing for development measurements of the BigQuery suite."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

ENVIRONMENT = "DBT_DQM_BIGQUERY_TIMING_DIRECTORY"


@contextmanager
def timed(phase, detail=""):
    """Append one timing record when a measurement directory is configured; otherwise do nothing."""
    directory = os.environ.get(ENVIRONMENT)
    if not directory:
        yield
        return
    started = time.monotonic()
    try:
        yield
    finally:
        test, _, stage = os.environ.get("PYTEST_CURRENT_TEST", "").rpartition(" (")
        record = {"test": test.rpartition("::")[2], "stage": stage.rstrip(")"), "phase": phase,
                  "detail": detail, "seconds": round(time.monotonic() - started, 3)}
        with (Path(directory) / f"{os.getpid()}.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps(record) + "\n")
