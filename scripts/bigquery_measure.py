"""Measure a BigQuery acceptance selection during development. This is never release evidence."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import release_evidence
from query_metrics import job_metrics

ROOT = Path(__file__).resolve().parents[1]
TIMING_ENVIRONMENT = "DBT_DQM_BIGQUERY_TIMING_DIRECTORY"
BYTE_LIMIT_NOTE = ("Requested per query only. dbt 2.0.11 did not enforce it on tested paths, some app "
                   "queries are outside it, and it is not a total spending ceiling.")


def command(*args):
    return subprocess.run(args, cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def selection_cases(selection):
    """Return the module, exact case names and pytest arguments for a selection."""
    if selection == "full":
        return "test_bigquery.py", release_evidence.source_names("test_bigquery.py"), []
    if selection == "smoke":
        return "test_bigquery.py", set(release_evidence.BIGQUERY_SMOKE_TESTS), ["-m", "bigquery_smoke"]
    if selection == "upgrade":
        return "test_identity_upgrade.py", release_evidence.source_names("test_identity_upgrade.py"), []
    raise ValueError("Unknown BigQuery selection")


def report_counts(junit, expected, filename):
    """Count outcomes, refusing a report that is not exactly the requested selection."""
    cases = list(ET.parse(junit).getroot().iter("testcase"))
    names = [case.attrib["name"] for case in cases]
    if len(names) != len(set(names)) or set(names) != expected:
        raise ValueError("Incorrect or incomplete test selection")
    if any(case.attrib.get("classname", "").split(".")[-1] != filename[:-3] for case in cases):
        raise ValueError("JUnit report came from the wrong acceptance module")
    counts = {"collected": len(cases), "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for case in cases:
        counts[next((key for tag, key in (("failure", "failed"), ("error", "errors"),
                     ("skipped", "skipped")) if case.find(tag) is not None), "passed")] += 1
    return counts


def summarize_timings(directory):
    """Aggregate per-phase records. Setup includes the fixture's dbt seed, so totals overlap."""
    tests = {}
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            row = tests.setdefault(record["test"], {
                "setup_seconds": 0.0, "teardown_seconds": 0.0, "dbt_subprocesses": 0, "dbt_seconds": 0.0,
                "dbt_dqm_runs": 0, "dbt_dqm_seconds": 0.0, "queries": 0, "query_seconds": 0.0})
            seconds = record["seconds"]
            if record["stage"] in ("setup", "teardown"):
                row[record["stage"] + "_seconds"] += seconds
            if record["phase"] == "dbt":
                row["dbt_subprocesses"] += 1
                row["dbt_seconds"] += seconds
            elif record["phase"] == "dbt-dqm":
                row["dbt_dqm_runs"] += 1
                row["dbt_dqm_seconds"] += seconds
            elif record["phase"] == "query":
                row["queries"] += 1
                row["query_seconds"] += seconds
    tests = {name: {key: round(value, 3) for key, value in row.items()} for name, row in sorted(tests.items())}
    totals = {}
    for row in tests.values():
        for key, value in row.items():
            totals[key] = round(totals.get(key, 0) + value, 3)
    return {"tests": tests, "totals": totals}


def engine_version(executable):
    match = re.search(r"(?:installed:\s*|\bdbt\s+)(\d+\.\d+\.\d+)", command(str(executable), "--version"))
    if not match:
        raise ValueError("Could not read the dbt engine version")
    return match[1]


def measure(selection, executable, workers, directory):
    """Run one selection once and retain its output, whether it passes or fails."""
    if ROOT == directory.resolve() or ROOT in directory.resolve().parents:
        raise ValueError("Keep measurement output outside the repository")
    if selection == "upgrade" and workers:
        raise ValueError("The cross-engine upgrade test runs serially")
    if workers < 0:
        raise ValueError("Workers cannot be negative")
    project = os.environ.get("DBT_DQM_BIGQUERY_TEST_PROJECT")
    if not project:
        raise ValueError("DBT_DQM_BIGQUERY_TEST_PROJECT is required")
    if selection == "upgrade" and not (os.environ.get("DBT_DQM_UPGRADE_CORE_EXECUTABLE")
                                       and os.environ.get("DBT_DQM_UPGRADE_V2_EXECUTABLE")):
        raise ValueError("The upgrade selection requires both engine executables")
    maximum = int(os.environ.get("DBT_DQM_BIGQUERY_MAX_BYTES_BILLED", str(1024 ** 3)))
    if maximum <= 0:
        raise ValueError("The acceptance query byte limit must be positive")
    filename, expected, selector = selection_cases(selection)
    executable = executable.resolve()
    version = engine_version(executable)
    validation_id = uuid.uuid4().hex
    started = datetime.now(UTC)
    run_directory = directory / f"{started:%Y%m%dT%H%M%S}-{selection}-{version}-{validation_id[:8]}"
    timings = run_directory / "timings"
    timings.mkdir(parents=True, mode=0o700)
    arguments = [sys.executable, "-m", "pytest", "-q", "-rA", "--durations=25", *selector]
    if workers:
        arguments += ["-n", str(workers), "--dist", "load"]
    arguments += [str(ROOT / "integration_tests/acceptance" / filename),
                  "--junitxml=" + str(run_directory / "junit.xml")]
    environment = {**os.environ, "DBT_DQM_DBT_EXECUTABLE": str(executable),
                   "DBT_DQM_VALIDATION_RUN_ID": validation_id, TIMING_ENVIRONMENT: str(timings),
                   "PATH": str(executable.parent) + os.pathsep + os.environ["PATH"]}
    with (run_directory / "pytest.log").open("w") as log:
        code = subprocess.run(arguments, cwd=ROOT, env=environment, stdout=log,
                              stderr=subprocess.STDOUT, check=False).returncode
    finished = datetime.now(UTC)
    result = {"kind": "development_measurement", "release_evidence": False, "selection": selection,
              "engine_version": version, "workers": workers, "sha": command("git", "rev-parse", "HEAD"),
              "tree": command("git", "rev-parse", "HEAD^{tree}"),
              "worktree_dirty": bool(command("git", "status", "--porcelain", "--untracked-files=no")),
              "started_at": started.isoformat(), "finished_at": finished.isoformat(),
              "wall_seconds": round((finished - started).total_seconds(), 1), "returncode": code,
              "status": "failed", "maximum_bytes_billed_requested": maximum,
              "maximum_bytes_billed_note": BYTE_LIMIT_NOTE, "validation_id": validation_id}
    try:
        result["counts"] = report_counts(run_directory / "junit.xml", expected, filename)
        if code == 0 and result["counts"]["passed"] == len(expected):
            result["status"] = "passed"
    except (ValueError, OSError, ET.ParseError) as error:
        result["validation_error"] = str(error)
    result["timing"] = summarize_timings(timings)
    result["query_metrics"] = job_metrics(project, validation_id, started, finished)
    (run_directory / "measurement.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
    print(f"{selection} on dbt {version}: {result['status']}; output: {run_directory}")
    return 0 if result["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", choices=("full", "smoke", "upgrade"), required=True)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    try:
        return measure(args.selection, args.engine, args.workers, args.directory)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Measurement refused: {error}\n")


if __name__ == "__main__":
    sys.exit(main())
