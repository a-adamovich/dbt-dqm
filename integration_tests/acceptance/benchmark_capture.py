"""Reproducible live BigQuery capture benchmark: 15 tests and 264 failed rows."""
from __future__ import annotations

import argparse
import json
import os
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    project_id = os.environ["DBT_DQM_BIGQUERY_TEST_PROJECT"]
    client = bigquery.Client(project=project_id, location="US")
    dataset_id = project_id + "." + PREFIX + "benchmark_" + uuid.uuid4().hex[:12]
    dataset = bigquery.Dataset(dataset_id)
    dataset.location = "US"
    dataset.labels = labels(uuid.uuid4().hex)
    client.create_dataset(dataset)
    try:
        with tempfile.TemporaryDirectory(prefix="dqm benchmark ") as directory:
            project = Path(directory)
            (project / "tests").mkdir()
            (project / "macros").mkdir()
            (project / "macros/naming.sql").write_text("{% macro generate_schema_name(custom_schema_name,node) %}{{ target.schema }}{% endmacro %}")
            (project / "dbt_packages").mkdir()
            (project / "dbt_packages/dbt_dqm").symlink_to(ROOT, target_is_directory=True)
            (project / "packages.yml").write_text(yaml.safe_dump({"packages": [{"local": str(ROOT)}]}))
            (project / "dbt_project.yml").write_text(yaml.safe_dump({"name": "benchmark", "profile": "benchmark",
                "config-version": 2, "version": "1.0.0", "test-paths": ["tests"]}))
            (project / "profiles.yml").write_text(yaml.safe_dump({"benchmark": {"target": "dev", "outputs": {
                "dev": {"type": "bigquery", "method": "oauth", "project": project_id,
                        "dataset": dataset.dataset_id, "location": "US", "threads": 4}}}}))
            for index in range(15):
                count = 18 if index < 14 else 12
                (project / f"tests/benchmark_{index:02}.sql").write_text("""
{{ config(enabled=var('dqm_enabled',false),store_failures=true,limit=100,severity='warn',
  meta={'dbt_dqm':{'granularity':['id']}}) }}
select cast(id as string) as id,concat('failed row ',cast(id as string)) as reason,
       'attribute' as attribute from unnest(generate_array(1,""" + str(count) + ")) id")
            started = time.monotonic()
            result = subprocess.run([sys.executable, "-m", "dbt_dqm_app.cli", "run", "--project-dir", str(project),
                                     "--profiles-dir", str(project), "--threads", "4"],
                                    capture_output=True, text=True, timeout=900, check=False)
            args.output.with_suffix(".log").write_text(result.stdout + result.stderr, encoding="utf-8")
            if result.returncode:
                raise RuntimeError(f"Benchmark failed; see {args.output.with_suffix('.log')}")
            metrics = json.loads((project / "target/dqm_workflow_metrics.json").read_text())
            totals = dict(next(iter(client.query(f"select count(*) as tests,sum(failure_count) as raw_rows,"
                         f"sum(captured_issue_count) as issues from `{dataset_id}.dqm_test_executions`").result())).items())
            assert totals == {"tests": 15, "raw_rows": 264, "issues": 264}, totals
            children = []
            for job_id in metrics["query_jobs"].get("collect_results", []):
                children.extend(client.list_jobs(parent_job=job_id))
            report = {"totals": totals, "wall_seconds": time.monotonic() - started, **metrics,
                      "capture_child_statement_count": len(children),
                      "capture_statement_types": [job.statement_type for job in children]}
            args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps(report, indent=2))
    finally:
        client.delete_dataset(dataset_id, delete_contents=True, not_found_ok=True)


if __name__ == "__main__":
    main()
