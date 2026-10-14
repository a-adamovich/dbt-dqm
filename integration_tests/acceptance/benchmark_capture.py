"""BigQuery workflow benchmark: matched 15-test/264-row scenarios and optional a4 baseline."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import yaml
from bigquery_datasets import PREFIX, labels
from google.cloud import bigquery

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ("fresh", "full_refresh", "unchanged", "retention_backlog")


def make_project(project, package, project_id, dataset_id):
    (project / "tests").mkdir()
    (project / "macros").mkdir()
    (project / "macros/naming.sql").write_text(
        "{% macro generate_schema_name(custom_schema_name,node) %}{{ target.schema }}{% endmacro %}")
    (project / "dbt_packages").mkdir()
    installed = project / "dbt_packages/dbt_dqm"
    installed.mkdir()
    shutil.copy2(package / "dbt_project.yml", installed / "dbt_project.yml")
    for directory in ("macros", "models", "tests"):
        shutil.copytree(package / directory, installed / directory,
                        ignore=shutil.ignore_patterns("__pycache__", "*.py"))
    (project / "packages.yml").write_text(yaml.safe_dump({"packages": [{"local": str(package)}]}))
    (project / "dbt_project.yml").write_text(yaml.safe_dump({
        "name": "benchmark", "profile": "benchmark", "config-version": 2,
        "version": "1.0.0", "test-paths": ["tests"],
        "vars": {"dbt_dqm_retention_days": 30, "dbt_dqm_event_retention_days": 30}}))
    (project / "profiles.yml").write_text(yaml.safe_dump({"benchmark": {"target": "dev", "outputs": {
        "dev": {"type": "bigquery", "method": "oauth", "project": project_id,
                "dataset": dataset_id, "location": "US", "threads": 4}}}}))
    for index in range(15):
        count = 18 if index < 14 else 12
        (project / f"tests/benchmark_{index:02}.sql").write_text("""
{{ config(enabled=var('dqm_enabled',false),store_failures=true,limit=100,severity='warn',
  meta={'dbt_dqm':{'granularity':['id']}}) }}
select cast(id as string) as id,concat('failed row ',cast(id as string)) as reason,
       'attribute' as attribute from unnest(generate_array(1,""" + str(count) + ")) id")


def age_backlog(client, dataset_id):
    # Only this trial's disposable fixture is aged. New executions retain their actual times.
    for table, column in (("dqm_test_executions", "captured_at"),
                          ("dqm_issue_observations", "observed_at"),
                          ("dqm_issue_events", "event_at"),
                          ("dqm_reconciliation_runs", "completed_at"),
                          ("dqm_maintenance_log", "logged_at")):
        client.query(f"update `{dataset_id}.{table}` set {column}=timestamp_sub(current_timestamp(),"
                     f"interval 60 day) where {column} is not null").result()
    client.query(f"insert into `{dataset_id}.dqm_app_change_staging` (upload_id,staged_at) "
                 "values ('benchmark-expired',timestamp_sub(current_timestamp(),interval 2 day))").result()


def run_trial(client, project_id, python, package, shortcuts, scenario, log_path):
    dataset_id = project_id + "." + PREFIX + "benchmark_" + uuid.uuid4().hex[:12]
    dataset = bigquery.Dataset(dataset_id)
    dataset.location = "US"
    dataset.labels = labels(uuid.uuid4().hex)
    client.create_dataset(dataset)
    try:
        with tempfile.TemporaryDirectory(prefix="dqm benchmark ") as directory:
            project = Path(directory)
            make_project(project, package, project_id, dataset.dataset_id)
            command = [str(python), "-m", "dbt_dqm_app.cli", "run", "--project-dir", str(project),
                       "--profiles-dir", str(project), "--target", "dev", "--threads", "4"]
            environment = {**os.environ, "DBT_PROFILE": "benchmark",
                           "DBT_TARGET_PATH": str(project / "target"),
                           "DBT_LOG_PATH": str(project / "logs"),
                           "DBT_DEFER": "false", "DBT_FAVOR_STATE": "false"}
            for key in ("DBT_STATE", "DBT_DEFER_STATE"):
                environment.pop(key, None)
            if shortcuts:
                command += ["--vars", "{dbt_dqm_maintenance_shortcuts: true}"]

            supports_yes = "--yes" in subprocess.check_output(
                [str(python), "-m", "dbt_dqm_app.cli", "run", "--help"], text=True)

            def invoke(options=()):
                if "--full-refresh" in options and supports_yes:
                    options = [*options, "--yes"]
                result = subprocess.run([*command, *options], capture_output=True, text=True,
                                        timeout=1200, check=False, cwd=project, env=environment)
                with log_path.open("a", encoding="utf-8") as log:
                    log.write(result.stdout + result.stderr)
                if result.returncode:
                    raise RuntimeError(f"Benchmark failed; see {log_path}")

            if scenario != "fresh":
                invoke()
            if scenario == "retention_backlog":
                age_backlog(client, dataset_id)
            started = time.monotonic()
            invoke(["--full-refresh"] if scenario == "full_refresh" else [])
            wall_seconds = time.monotonic() - started
            metrics = json.loads((project / "target/dqm_workflow_metrics.json").read_text())
            invocation = json.loads((project / "target/dqm_run_results.json").read_text())["metadata"]["invocation_id"]
            totals = dict(next(iter(client.query(
                f"select count(*) as tests,sum(failure_count) as raw_rows,sum(captured_issue_count) as issues "
                f"from `{dataset_id}.dqm_test_executions` where invocation_id=@invocation",
                job_config=bigquery.QueryJobConfig(query_parameters=[
                    bigquery.ScalarQueryParameter("invocation", "STRING", invocation)])).result())).items())
            assert totals == {"tests": 15, "raw_rows": 264, "issues": 264}, totals
            active = next(iter(client.query(f"select count(*) as n from `{dataset_id}.dqm_issue_occurrences` "
                                            "where record_status='Active'").result())).n
            assert active == 264, active
            jobs, statements = {}, {}
            capture_types = []
            for phase, identifiers in metrics["query_jobs"].items():
                identifiers = sorted(set(identifiers))
                jobs[phase] = len(identifiers)
                children = [job for identifier in identifiers for job in client.list_jobs(parent_job=identifier)]
                statements[phase] = len(children)
                if phase == "collect_results":
                    capture_types = [job.statement_type for job in children]
            return {"totals": totals, "wall_seconds": wall_seconds, **metrics,
                    "job_counts": jobs, "child_statement_counts": statements,
                    "capture_child_statement_count": statements.get("collect_results", 0),
                    "capture_statement_types": capture_types}
    finally:
        client.delete_dataset(dataset_id, delete_contents=True, not_found_ok=True)


def summarize(samples):
    return {"median_wall_seconds": statistics.median(row["wall_seconds"] for row in samples),
            **{f"median_{field}": {
                key: statistics.median(row[field].get(key, 0) for row in samples)
                for key in sorted({key for row in samples for key in row[field]})}
               for field in ("phase_seconds", "sql_calls", "job_counts", "child_statement_counts")}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=list(SCENARIOS))
    parser.add_argument("--baseline-python", type=Path,
                        help="Python from an installed 0.2.0a4 environment")
    parser.add_argument("--baseline-package", type=Path,
                        help="Separate checkout of the v0.2.0a4 dbt package")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    if bool(args.baseline_python) != bool(args.baseline_package):
        parser.error("Provide both --baseline-python and --baseline-package")
    if not os.environ.get("DBT_DQM_BIGQUERY_TEST_PROJECT"):
        parser.error("Set DBT_DQM_BIGQUERY_TEST_PROJECT and configure ADC")
    variants = [("candidate_default", Path(sys.executable), ROOT, False),
                ("candidate_shortcuts", Path(sys.executable), ROOT, True)]
    if args.baseline_python:
        version = subprocess.check_output(
            [str(args.baseline_python.resolve()), "-c",
             "import dbt_dqm_app; print(dbt_dqm_app.__version__)"], text=True,
            cwd=tempfile.gettempdir()).strip()
        package_version = yaml.safe_load((args.baseline_package / "dbt_project.yml").read_text())["version"]
        if version != "0.2.0a4" or package_version != "0.2.0-a4":
            parser.error("The baseline interpreter and dbt package must both be 0.2.0a4")
        variants.insert(0, ("released_a4", args.baseline_python.resolve(),
                            args.baseline_package.resolve(), False))
    project_id = os.environ["DBT_DQM_BIGQUERY_TEST_PROJECT"]
    client = bigquery.Client(project=project_id, location="US")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {"repeats": args.repeats, "baseline_included": bool(args.baseline_python), "scenarios": {}}
    for scenario in args.scenarios:
        report["scenarios"][scenario] = {}
        for variant, python, package, shortcuts in variants:
            samples = []
            for repeat in range(args.repeats):
                print(f"{scenario}: {variant}, trial {repeat+1}/{args.repeats}", flush=True)
                log_path = args.output.with_name(f"{args.output.stem}-{scenario}-{variant}-{repeat+1}.log")
                samples.append(run_trial(client, project_id, python, package, shortcuts, scenario, log_path))
                report["scenarios"][scenario][variant] = {"summary": summarize(samples), "samples": samples}
                args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
