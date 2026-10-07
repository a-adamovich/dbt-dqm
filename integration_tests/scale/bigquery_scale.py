"""Measure dbt-dqm reconciliation and app sync cost on BigQuery as history grows.

Usage (creates and deletes its own dataset in the project; billable but small):

    GOOGLE_APPLICATION_CREDENTIALS=... DBT_DQM_BIGQUERY_TEST_PROJECT=my-project \\
      uv run python integration_tests/scale/bigquery_scale.py --output scale-results

It builds a temporary consumer project with `--tests` extra tracked tests, initializes the DQM
schema through the package, then:

1. loads synthetic processed history up to `--first-occurrences`, and measures a steady
   reconciliation (three demo tests ran since the last one);
2. grows history to `--occurrences` and measures the same steady reconciliation again;
3. measures one app sync (fresh process: wall time, peak RSS);
4. measures a mass pass, in which every scale test passes and all their Active issues archive.

Cost is read from INFORMATION_SCHEMA.JOBS_BY_USER for the jobs created during each step. Script
parent jobs repeat their children's totals, so only non-script jobs are summed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import yaml
from google.cloud import bigquery

ROOT = Path(__file__).resolve().parents[2]
PROFILE = "dbt_dqm_demo"
DEMO_TESTS = ("demo_customer_email_invalid", "demo_order_amount_invalid", "demo_order_line_invalid")
SCALE_TEST = """{{ config(tags=['dqm'], store_failures=true, meta={'dbt_dqm': {'granularity': ['customer_id']}}) }}
select customer_id from {{ ref('demo_records') }} where false
"""
SIGNATURE = '{"granularity":["customer_id"],"version":"dqm-id-v1"}'


def payload_sql(columns: int, key: str = "g") -> str:
    """The grain plus `columns` ~40-character context fields, as in the Postgres harness."""
    parts = [f"'\"customer_id\":\"c-', cast({key} as string), '\"'"]
    for index in range(columns):
        parts.append(
            f"',\"context_{index:02d}\":\"', to_hex(md5(concat(cast({key} as string), '{index}'))), '\"'"
        )
    return "concat('{', " + ", ".join(parts) + ", '}')"


class Harness:
    def __init__(self, workdir: Path, project: str, location: str, tests: int) -> None:
        self.client = bigquery.Client(project=project, location=location)
        self.project, self.location = project, location
        self.dataset = "dqm_scale_" + uuid.uuid4().hex[:10]
        dataset = bigquery.Dataset(f"{project}.{self.dataset}")
        dataset.location = location
        self.client.create_dataset(dataset)
        self.path = workdir / "project"
        shutil.copytree(
            ROOT / "integration_tests/demo_bigquery",
            self.path,
            ignore=shutil.ignore_patterns("target", "logs", "dbt_packages"),
        )
        (self.path / "dbt_packages").mkdir()
        (self.path / "dbt_packages/dbt_dqm").symlink_to(ROOT, target_is_directory=True)
        for index in range(tests):
            (self.path / f"tests/scale_t{index:04d}.sql").write_text(SCALE_TEST)
        self.profiles = workdir / "profiles"
        self.profiles.mkdir()
        output = {
            "type": "bigquery",
            "method": "oauth",
            "project": project,
            "dataset": self.dataset,
            "location": location,
            "threads": 8,
        }
        (self.profiles / "profiles.yml").write_text(
            yaml.safe_dump({PROFILE: {"target": "dev", "outputs": {"dev": output}}})
        )

    def table(self, name: str) -> str:
        return f"`{self.project}.{self.dataset}.{name}`"

    def sql(self, statement: str) -> list[dict]:
        return [dict(row.items()) for row in self.client.query(statement).result()]

    def dbt(self, *args: str) -> None:
        command = [
            str(Path(sys.executable).with_name("dbt")),
            *args,
            "--project-dir",
            str(self.path),
            "--profiles-dir",
            str(self.profiles),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"dbt {' '.join(args)} failed:\n{result.stdout}{result.stderr}")

    def measured(self, action) -> dict:
        """Wall time and BigQuery job cost of everything `action` runs."""
        started_at = datetime.now(UTC)
        started = time.perf_counter()
        extra = action() or {}
        seconds = time.perf_counter() - started
        time.sleep(5)  # Job metadata is written asynchronously.
        cost = self.sql(f"""
            select count(*) as jobs,
              coalesce(sum(total_bytes_processed), 0) as bytes_processed,
              coalesce(sum(total_bytes_billed), 0) as bytes_billed,
              coalesce(sum(total_slot_ms), 0) as slot_ms
            from `region-{self.location.lower()}`.INFORMATION_SCHEMA.JOBS_BY_USER
            where creation_time >= timestamp('{started_at.isoformat()}')
              and creation_time <= current_timestamp()
              and coalesce(statement_type, '') != 'SCRIPT'
              and not starts_with(query, 'select count(*) as jobs')
        """)[0]
        return {
            "seconds": round(seconds, 1),
            "jobs": cost["jobs"],
            "gb_processed": round(cost["bytes_processed"] / 1e9, 3),
            "gb_billed": round(cost["bytes_billed"] / 1e9, 3),
            "slot_seconds": round(cost["slot_ms"] / 1000, 1),
            **extra,
        }

    def reconcile(self) -> None:
        self.dbt("run", "--select", "dqm_reconcile")

    def load_history(
        self, first: int, count: int, args: argparse.Namespace, ids: list[str]
    ) -> None:
        """Processed synthetic history for occurrences (first, first+count], older than real runs."""
        executions = max(1, args.executions * count // args.occurrences)
        observations = args.observations * count // args.occurrences
        chunk = max(1, -(-observations // 1000))
        active_limit = int(args.occurrences * args.active_fraction)
        step = 2 * 365 * 86400 / args.occurrences
        id_array = "[" + ",".join(f"'{test_id}'" for test_id in ids) + "]"
        test_of = f"{id_array}[offset(mod(%s, {len(ids)}))]"
        payload = payload_sql(args.payload_columns)
        e0 = args.executions * first // args.occurrences
        self.sql(f"""
            declare anchor timestamp default (
              select timestamp_sub(min(captured_at), interval 1 day) from {self.table("dqm_test_executions")});
            insert into {self.table("dqm_test_executions")}
              (invocation_id, captured_at, command, test_unique_id, test_name, test_status,
               failure_count, collection_status, granularity_signature, identity_scheme_signature,
               capture_mode, owner_conflict_identity_count)
            select concat('syn-exec-', cast(e as string)),
              timestamp_sub(anchor, interval cast(e * 600 as int64) second), 'test',
              {test_of % "e"}, 'scale_synthetic', 'fail', 1, 'success', '["customer_id"]',
              '{SIGNATURE}', 'identity_only', 0
            from unnest(generate_array({e0 + 1}, {e0 + executions})) e;

            insert into {self.table("dqm_reconciliation_receipts")}
              (test_unique_id, invocation_id, captured_at, run_id, outcome, recorded_at)
            select test_unique_id, invocation_id, captured_at, 'syn-run', 'applied', captured_at
            from {self.table("dqm_test_executions")}
            where starts_with(invocation_id, 'syn-exec-')
              and cast(substr(invocation_id, 10) as int64) between {e0 + 1} and {e0 + executions};

            -- Observation o belongs to identity first+1+mod(o, count), seen on the pass
            -- div(o, count); (identity, pass) pairs are unique.
            insert into {self.table("dqm_issue_observations")}
              (invocation_id, observed_at, test_unique_id, test_name, unique_id, record_values_json,
               failure_row_count, test_tags, owner_conflict)
            select concat('syn-exec-', cast(e as string)),
              timestamp_sub(anchor, interval cast(o as int64) second), {test_of % "e"},
              'scale_synthetic', to_hex(md5(concat('identity-', cast(g as string)))), {payload}, 1,
              '["dqm"]', 0
            from (
              select o, {first} + 1 + mod(o, {count}) as g,
                {e0 + 1} + mod(div(o, {count}), {executions}) as e
              from unnest(generate_array(0, 999)) a, unnest(generate_array(0, {chunk - 1})) b,
                unnest([a * {chunk} + b]) o
              where o < {observations}
            );

            insert into {self.table("dqm_issue_occurrences")}
              (occurrence_id, test_unique_id, test_name, unique_id, occurrence_number,
               record_values_json, failure_row_count, test_tags, first_seen_at, last_seen_at,
               archived_at, record_status, close_reason, first_seen_invocation,
               last_seen_invocation, last_evaluated_invocation, workflow_status, test_status,
               annotation_version, granularity_signature, identity_scheme_signature, review_verdict)
            select to_hex(md5(concat('occurrence-', cast(g as string)))), {test_of % "g"},
              'scale_synthetic', to_hex(md5(concat('identity-', cast(g as string)))), 1, {payload},
              1, '["dqm"]',
              timestamp_sub(anchor, interval cast(g * {step} as int64) second),
              timestamp_sub(anchor, interval cast(g * {step} - 3600 as int64) second),
              if(g <= {active_limit}, null,
                 timestamp_sub(anchor, interval cast(g * {step} - 7200 as int64) second)),
              if(g <= {active_limit}, 'Active', 'Archived'),
              if(g <= {active_limit}, null, 'Passed'),
              'syn-exec-1', 'syn-exec-1', 'syn-exec-1', 'NEW', 'NEW', 0, '["customer_id"]',
              '{SIGNATURE}', 'UNREVIEWED'
            from unnest(generate_array({first + 1}, {first + count})) g;
        """)

    def app_sync(self) -> dict:
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("app_memory.py")),
                "--project-dir",
                str(self.path),
                "--profiles-dir",
                str(self.profiles),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout.strip().splitlines()[-1])

    def drop(self) -> None:
        self.client.delete_dataset(
            f"{self.project}.{self.dataset}", delete_contents=True, not_found_ok=True
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tests", type=int, default=200)
    parser.add_argument("--first-occurrences", type=int, default=100_000)
    parser.add_argument("--occurrences", type=int, default=1_000_000)
    parser.add_argument("--active-fraction", type=float, default=0.1)
    parser.add_argument("--executions", type=int, default=50_000)
    parser.add_argument("--observations", type=int, default=5_000_000)
    parser.add_argument("--payload-columns", type=int, default=8)
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--output", type=Path, default=Path("scale-results"))
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    project = os.environ["DBT_DQM_BIGQUERY_TEST_PROJECT"]
    location = os.environ.get("DBT_DQM_BIGQUERY_TEST_LOCATION", "US")
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as workdir:
        harness = Harness(Path(workdir), project, location, args.tests)
        report: dict = {"adapter": "bigquery", "label": args.label, "volumes": vars(args).copy()}
        report["volumes"]["output"] = str(args.output)
        try:
            harness.dbt("seed")
            harness.dbt("run", "--select", "demo_records")
            harness.dbt("test", "--select", "tag:dqm")
            harness.dbt("run", "--select", "package:dbt_dqm")
            ids = [
                row["test_unique_id"]
                for row in harness.sql(
                    f"select distinct test_unique_id from {harness.table('dqm_test_executions')} "
                    "where starts_with(test_name, 'scale_t') order by 1"
                )
            ]
            first = args.first_occurrences
            report["load_first"] = harness.measured(
                lambda: harness.load_history(0, first, args, ids)
            )
            harness.dbt("test", "--select", *DEMO_TESTS)
            report["steady_first"] = harness.measured(harness.reconcile)
            report["load_rest"] = harness.measured(
                lambda: harness.load_history(first, args.occurrences - first, args, ids)
            )
            harness.dbt("test", "--select", *DEMO_TESTS)
            report["steady_full"] = harness.measured(harness.reconcile)
            report["app_sync"] = harness.measured(lambda: {"process": harness.app_sync()})
            harness.dbt("test", "--select", "tag:dqm")
            report["mass_pass"] = harness.measured(harness.reconcile)
            report["active_scale_after_mass_pass"] = harness.sql(
                f"select count(*) n from {harness.table('dqm_issue_occurrences')} "
                "where test_name='scale_synthetic' and record_status='Active'"
            )[0]["n"]
        finally:
            if not args.keep:
                harness.drop()
    path = args.output / f"bigquery-{args.label}.json"
    path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
