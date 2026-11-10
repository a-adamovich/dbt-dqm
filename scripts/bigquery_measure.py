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
CONSOLIDATION_NOTE = ("Timings cover only the cases in this selection. They cannot justify merging a case "
                      "that was not selected; see docs/ci-measurements.md.")
PACKAGE_DEPLOYMENTS = {"build --select package:dbt_dqm", "run --select package:dbt_dqm"}
FIXTURE_STEPS = {"create dataset": "fixture_dataset_seconds", "prepare project": "fixture_project_seconds",
                 "seed": "fixture_seed_seconds"}


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


def case_durations(junit):
    """Return each case's total time from JUnit, or nothing if the run left no readable report."""
    try:
        return {case.attrib["name"]: float(case.attrib["time"]) for case in ET.parse(junit).getroot().iter("testcase")}
    except (OSError, ET.ParseError, KeyError, ValueError):
        return {}


def new_row():
    return {"case_seconds": None, "fixture_setup_seconds": 0.0, "fixture_dataset_seconds": 0.0,
            "fixture_project_seconds": 0.0, "fixture_seed_seconds": 0.0, "installation_seconds": 0.0,
            "installation_steps": [], "installation_includes_reconcile": False,
            "initialize_repeat_seconds": 0.0, "teardown_seconds": 0.0, "scenario_seconds": None,
            "dbt_subprocesses": 0, "dbt_seconds": 0.0, "dbt_dqm_runs": 0, "dbt_dqm_seconds": 0.0,
            "queries": 0, "query_seconds": 0.0, "runner_metrics_missing": 0, "runner_phase_seconds": {},
            "runner_sql_calls": {}, "runner_query_jobs": {}}


class Installation:
    """Attribute only a case's first tracking setup to installation, in the order it happened."""

    def __init__(self, row):
        self.row, self.initialized, self.deployed, self.captured = row, False, False, False

    def initialize(self, seconds, source):
        if self.initialized:
            self.row["initialize_repeat_seconds"] += seconds
            return
        self.initialized = True
        self.row["installation_seconds"] += seconds
        self.row["installation_steps"].append(f"{source}: initialize_dqm")

    def deploy(self, seconds, step):
        if self.deployed:
            return
        self.deployed = True
        self.row["installation_seconds"] += seconds
        self.row["installation_steps"].append(step)
        self.row["installation_includes_reconcile"] = self.captured

    def harness(self, detail, seconds):
        if detail == "dbt_dqm.initialize_dqm":
            self.initialize(seconds, "harness")
        elif detail == "dbt_dqm.collect_results":
            self.captured = True
        elif detail in PACKAGE_DEPLOYMENTS:
            self.deploy(seconds, f"harness: {detail}")

    def runner(self, phases):
        if "initialize_dqm" in phases:
            self.initialize(phases["initialize_dqm"], "runner")
        self.captured = self.captured or "collect_results" in phases
        if "run" in phases:
            self.deploy(phases["run"], "runner: run")


def add_runner_metrics(row, installation, runner):
    if runner is None:
        row["runner_metrics_missing"] += 1
        return
    phases = runner.get("phase_seconds") or {}
    installation.runner(phases)
    for phase, seconds in phases.items():
        row["runner_phase_seconds"][phase] = row["runner_phase_seconds"].get(phase, 0.0) + seconds
    for phase, calls in (runner.get("sql_calls") or {}).items():
        row["runner_sql_calls"][phase] = row["runner_sql_calls"].get(phase, 0) + calls
    for phase, jobs in (runner.get("query_jobs") or {}).items():
        row["runner_query_jobs"][phase] = row["runner_query_jobs"].get(phase, 0) + len(set(jobs))


def add_record(row, installation, record):
    seconds, phase, detail, stage = record["seconds"], record["phase"], record["detail"], record["stage"]
    if stage == "setup":
        row["fixture_setup_seconds"] += seconds
        if detail in FIXTURE_STEPS:
            row[FIXTURE_STEPS[detail]] += seconds
    elif stage == "teardown":
        row["teardown_seconds"] += seconds
    if phase == "dbt":
        row["dbt_subprocesses"] += 1
        row["dbt_seconds"] += seconds
        installation.harness(detail, seconds)
    elif phase == "dbt-dqm":
        row["dbt_dqm_runs"] += 1
        row["dbt_dqm_seconds"] += seconds
        add_runner_metrics(row, installation, record.get("runner"))
    elif phase == "query":
        row["queries"] += 1
        row["query_seconds"] += seconds


def finished_row(row, duration):
    """Round a row. An engine that reports no SQL calls or job ids shows null, never zero."""
    if duration is not None:
        row["case_seconds"] = duration
        row["scenario_seconds"] = (duration - row["fixture_setup_seconds"] - row["installation_seconds"]
                                   - row["teardown_seconds"])
    for key, value in row.items():
        if isinstance(value, float):
            row[key] = round(value, 3)
        elif isinstance(value, dict):
            row[key] = {name: round(item, 3) for name, item in sorted(value.items())} or None
    return row


def combined_totals(rows):
    totals = {"runner_phase_seconds": {}, "runner_sql_calls": {}, "runner_query_jobs": {}}
    for row in rows:
        for key, value in row.items():
            if isinstance(value, dict):
                for name, item in value.items():
                    totals[key][name] = round(totals[key].get(name, 0) + item, 3)
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[key] = round(totals.get(key, 0) + value, 3)
    return {key: (value or None) if isinstance(value, dict) else value for key, value in totals.items()}


def summarize_timings(directory, junit=None):
    """Aggregate per-phase records.

    The fixture's seed is in both fixture setup and dbt subprocess totals, and installation is
    part of the dbt and runner totals, so these columns overlap and must not be added together.
    """
    rows, installations = {}, {}
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            row = rows.setdefault(record["test"], new_row())
            add_record(row, installations.setdefault(record["test"], Installation(row)), record)
    durations = case_durations(junit) if junit else {}
    tests = {name: finished_row(row, durations.get(name)) for name, row in sorted(rows.items())}
    return {"tests": tests, "totals": combined_totals(tests.values())}


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
              "maximum_bytes_billed_note": BYTE_LIMIT_NOTE, "consolidation_note": CONSOLIDATION_NOTE,
              "validation_id": validation_id}
    try:
        result["counts"] = report_counts(run_directory / "junit.xml", expected, filename)
        if code == 0 and result["counts"]["passed"] == len(expected):
            result["status"] = "passed"
    except (ValueError, OSError, ET.ParseError) as error:
        result["validation_error"] = str(error)
    result["timing"] = summarize_timings(timings, run_directory / "junit.xml")
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
