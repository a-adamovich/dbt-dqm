"""Measure dbt-dqm reconciliation and app sync cost at realistic history sizes on PostgreSQL.

Usage (a disposable database; the script creates and drops its own schema):

    DBT_DQM_TEST_DSN='host=localhost port=5432 dbname=dbt_dqm_ci user=postgres password=...' \\
      uv run python integration_tests/scale/postgres_scale.py --output scale-results

It builds a temporary consumer project with `--tests` extra tracked tests, initializes the DQM
schema through the package, bulk-loads synthetic history (occurrences, executions, observations,
receipts), then measures:

* steady state: a reconciliation of a few new executions against the full history;
* mass pass: every scale test passes at once, archiving all of their Active occurrences;
* app sync: the verified issue read plus Health, with wall time and Python peak memory.

For the steady run it also records `EXPLAIN (ANALYZE, BUFFERS)` of the reconciliation change set.
The reconcile model's execution time spans the whole transaction, so it is also the time the
occurrence table's EXCLUSIVE lock is held.
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
import tracemalloc
import uuid
from pathlib import Path

import psycopg2
import yaml
from psycopg2.extensions import parse_dsn

ROOT = Path(__file__).resolve().parents[2]
PROJECT_NAME = "dbt_dqm_demo_postgres"
DEMO_TESTS = ("demo_customer_email_invalid", "demo_order_amount_invalid", "demo_order_line_invalid")

SCALE_TEST = """{{ config(tags=['dqm'], store_failures=true, meta={'dbt_dqm': {'granularity': ['customer_id']}}) }}
select customer_id from {{ ref('demo_records') }} where false
"""

EXPLAIN_MACRO = """
{% macro scale_explain() %}
  {% set inputs = dbt_dqm.dqm_relation('dqm_reconciliation_inputs') %}
  {% set executions = dbt_dqm.dqm_relation('dqm_test_executions') %}
  {% set receipts = dbt_dqm.dqm_relation('dqm_reconciliation_receipts') %}
  {% set freeze %}
    select 'scale-explain', execution.test_unique_id, execution.invocation_id, execution.captured_at
    from {{ executions }} execution
    where {{ dbt_dqm.conclusive_sql() }} and execution.test_unique_id in ({{ dbt_dqm.tracked_ids_sql() }})
      and not exists(select 1 from {{ receipts }} receipt
        where receipt.test_unique_id=execution.test_unique_id and receipt.invocation_id=execution.invocation_id)
  {% endset %}
  {% for name, sql in [('freeze', freeze), ('change_set', none)] %}
    {% if name == 'change_set' %}
      {% do run_query('insert into ' ~ inputs ~ ' (run_id,test_unique_id,invocation_id,captured_at) ' ~ freeze) %}
      {% set sql = dbt_dqm.reconcile_change_set_sql('scale-explain') %}
    {% endif %}
    {# Mirror reconcile_pre, which plans the change set without nested loops. #}
    {% do run_query('set enable_nestloop = ' ~ ('off' if name == 'change_set' else 'on')) %}
    {% set plan = run_query('explain (analyze, buffers) ' ~ sql) %}
    {{ log('SCALE-PLAN-BEGIN ' ~ name, info=true) }}
    {% for row in plan %}{{ log(row[0], info=true) }}{% endfor %}
    {{ log('SCALE-PLAN-END ' ~ name, info=true) }}
  {% endfor %}
  {% do run_query('delete from ' ~ inputs ~ " where run_id='scale-explain'") %}
{% endmacro %}
"""


class Project:
    def __init__(self, path: Path, profiles: Path, dsn: str, schema: str) -> None:
        self.path, self.profiles, self.dsn, self.schema = path, profiles, dsn, schema

    def dbt(self, *args: str, variables: dict | None = None) -> subprocess.CompletedProcess:
        command = [
            str(Path(sys.executable).with_name("dbt")),
            *args,
            "--project-dir",
            str(self.path),
            "--profiles-dir",
            str(self.profiles),
        ]
        if variables:
            command += ["--vars", json.dumps(variables)]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"dbt {' '.join(args)} failed:\n{result.stdout}{result.stderr}")
        return result

    def sql(self, statement: str, params=None) -> list[tuple]:
        with psycopg2.connect(self.dsn) as conn, conn.cursor() as cursor:
            cursor.execute(statement.replace("@schema", f'"{self.schema}"'), params)
            return cursor.fetchall() if cursor.description else []

    def reconcile_seconds(self) -> float:
        self.dbt("run", "--select", "dqm_reconcile")
        results = json.loads((self.path / "target/run_results.json").read_text())
        return next(
            r["execution_time"]
            for r in results["results"]
            if r["unique_id"] == "model.dbt_dqm.dqm_reconcile"
        )


def build_project(workdir: Path, dsn: str, schema: str, tests: int) -> Project:
    path = workdir / "project"
    shutil.copytree(
        ROOT / "integration_tests/demo_postgres",
        path,
        ignore=shutil.ignore_patterns("target", "logs", "dbt_packages"),
    )
    (path / "dbt_packages").mkdir()
    (path / "dbt_packages/dbt_dqm").symlink_to(ROOT, target_is_directory=True)
    (path / "packages.yml").write_text(yaml.safe_dump({"packages": [{"local": str(ROOT)}]}))
    for index in range(tests):
        (path / f"tests/scale_t{index:04d}.sql").write_text(SCALE_TEST)
    (path / "macros/scale_explain.sql").write_text(EXPLAIN_MACRO)
    params = parse_dsn(dsn)
    profiles = workdir / "profiles"
    profiles.mkdir()
    output = {
        "type": "postgres",
        "host": params.get("host", "localhost"),
        "port": int(params.get("port", 5432)),
        "user": params["user"],
        "password": params.get("password", ""),
        "dbname": params["dbname"],
        "schema": schema,
        "threads": 4,
    }
    (profiles / "profiles.yml").write_text(
        yaml.safe_dump({PROJECT_NAME: {"target": "dev", "outputs": {"dev": output}}})
    )
    return Project(path, profiles, dsn, schema)


def load_history(project: Project, args: argparse.Namespace) -> None:
    """Bulk-insert processed synthetic history for the scale tests, older than any real run."""
    scale_ids = [
        row[0]
        for row in project.sql(
            "select distinct test_unique_id from @schema.dqm_test_executions "
            "where test_name like 'scale_t%%' order by 1"
        )
    ]
    if len(scale_ids) != args.tests:
        raise RuntimeError(f"expected {args.tests} scale tests, found {len(scale_ids)}")
    ids = "array[" + ",".join(f"'{test_id}'" for test_id in scale_ids) + "]"
    active = int(args.occurrences * args.active_fraction)
    # Spread occurrence history over two years so the app's 90-day archive window holds a
    # realistic share of it.
    step = 2 * 365 * 86400 / args.occurrences
    # Synthetic timestamps end a day before the first real execution, so every real run stays
    # newer than the receipted history and no late-evidence check fires.
    project.sql(f"""
        create temporary table scale_base as
        select min(captured_at) - interval '1 day' as anchor from @schema.dqm_test_executions;

        insert into @schema.dqm_test_executions
          (invocation_id, captured_at, command, test_unique_id, test_name, test_status,
           failure_count, collection_status, granularity_signature, identity_scheme_signature,
           capture_mode, owner_conflict_identity_count)
        select 'syn-exec-' || g, (select anchor from scale_base) - (g * interval '10 minutes'),
          'test', ({ids})[1 + g % {args.tests}], 'scale_synthetic', 'fail', 1, 'success',
          '["customer_id"]', '{{"granularity":["customer_id"],"version":"dqm-id-v1"}}',
          'identity_only', 0
        from generate_series(1, {args.executions}) g;

        insert into @schema.dqm_reconciliation_receipts
          (test_unique_id, invocation_id, captured_at, run_id, outcome, recorded_at)
        select test_unique_id, invocation_id, captured_at, 'syn-run', 'applied', captured_at
        from @schema.dqm_test_executions where invocation_id like 'syn-exec-%%';

        insert into @schema.dqm_issue_observations
          (invocation_id, observed_at, test_unique_id, test_name, unique_id, record_values_json,
           failure_row_count, test_tags, owner_conflict)
        -- (identity, invocation) pairs are unique: identity = g mod occurrences, and the
        -- invocation advances once per pass over the identities.
        select 'syn-exec-' || (1 + (g / {args.occurrences}) % {args.executions}),
          (select anchor from scale_base) - (g * interval '1 minute'),
          ({ids})[1 + (1 + (g / {args.occurrences}) % {args.executions}) % {args.tests}],
          'scale_synthetic',
          md5('identity-' || (g % {args.occurrences})), '{{"customer_id":"c-' || g || '"}}', 1,
          '["dqm"]', 0
        from generate_series(0, {args.observations} - 1) g;

        insert into @schema.dqm_issue_occurrences
          (occurrence_id, test_unique_id, test_name, unique_id, occurrence_number,
           record_values_json, failure_row_count, test_tags, first_seen_at, last_seen_at,
           archived_at, record_status, close_reason, first_seen_invocation, last_seen_invocation,
           last_evaluated_invocation, workflow_status, test_status, annotation_version,
           granularity_signature, identity_scheme_signature, review_verdict)
        select md5('occurrence-' || g), ({ids})[1 + g % {args.tests}], 'scale_synthetic',
          md5('identity-' || g), 1, '{{"customer_id":"c-' || g || '"}}', 1, '["dqm"]',
          (select anchor from scale_base) - (g * {step} * interval '1 second'),
          (select anchor from scale_base) - (g * {step} * interval '1 second' - interval '1 hour'),
          case when g <= {active} then null
               else (select anchor from scale_base) - (g * {step} * interval '1 second' - interval '2 hours') end,
          case when g <= {active} then 'Active' else 'Archived' end,
          case when g <= {active} then null else 'Passed' end,
          'syn-exec-1', 'syn-exec-1', 'syn-exec-1', 'NEW', 'NEW', 0,
          '["customer_id"]', '{{"granularity":["customer_id"],"version":"dqm-id-v1"}}',
          'UNREVIEWED'
        from generate_series(1, {args.occurrences}) g;

        analyze @schema.dqm_test_executions;
        analyze @schema.dqm_reconciliation_receipts;
        analyze @schema.dqm_issue_observations;
        analyze @schema.dqm_issue_occurrences;
    """)


def explain(project: Project, output: Path, scenario: str) -> dict[str, float]:
    result = project.dbt("run-operation", "scale_explain")
    plans: dict[str, list[str]] = {}
    current = None
    for line in result.stdout.splitlines():
        text = line.split("  ", 1)[-1].strip() if "  " in line else line.strip()
        if text.startswith("SCALE-PLAN-BEGIN "):
            current = text.removeprefix("SCALE-PLAN-BEGIN ")
            plans[current] = []
        elif text.startswith("SCALE-PLAN-END"):
            current = None
        elif current is not None:
            plans[current].append(line.split("  ", 1)[-1] if "  " in line else line)
    times = {}
    for name, lines in plans.items():
        (output / f"postgres-plan-{scenario}-{name}.txt").write_text("\n".join(lines) + "\n")
        execution = [line for line in lines if "Execution Time" in line]
        times[name] = float(execution[-1].split(":")[1].split()[0]) / 1000 if execution else None
    return times


def app_sync(project: Project) -> dict[str, float | int]:
    from dbt_dqm_app.config import load_config
    from dbt_dqm_app.warehouse import fetch_health, fetch_issues

    config = load_config(project.path, project.profiles, "dev")
    tracemalloc.start()
    started = time.perf_counter()
    issues = fetch_issues(config)
    fetch_health(config)
    seconds = time.perf_counter() - started
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return {"seconds": round(seconds, 3), "issues": len(issues), "peak_mb": round(peak / 2**20, 1)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tests", type=int, default=200)
    parser.add_argument("--occurrences", type=int, default=1_000_000)
    parser.add_argument("--active-fraction", type=float, default=0.1)
    parser.add_argument("--executions", type=int, default=50_000)
    parser.add_argument("--observations", type=int, default=5_000_000)
    parser.add_argument("--output", type=Path, default=Path("scale-results"))
    parser.add_argument("--keep", action="store_true", help="keep the schema for inspection")
    parser.add_argument(
        "--explain-mass-pass",
        action="store_true",
        help="also EXPLAIN ANALYZE the mass-pass change set (runs it twice; small volumes only)",
    )
    args = parser.parse_args()
    dsn = os.environ["DBT_DQM_TEST_DSN"]
    args.output.mkdir(parents=True, exist_ok=True)
    schema = "dqm_scale_" + uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory() as workdir:
        project = build_project(Path(workdir), dsn, schema, args.tests)
        try:
            project.dbt("seed")
            project.dbt("run", "--select", "demo_records")
            project.dbt("test", "--select", "tag:dqm")
            project.dbt("run", "--select", "package:dbt_dqm")
            started = time.perf_counter()
            load_history(project, args)
            load_seconds = time.perf_counter() - started

            # Steady state: only the three demo tests ran since the last reconciliation.
            project.dbt("test", "--select", *DEMO_TESTS)
            plans = explain(project, args.output, "steady")
            steady = project.reconcile_seconds()
            sync = app_sync(project)

            # Mass pass: every scale test passes, archiving all of their Active occurrences.
            project.dbt("test", "--select", "tag:dqm")
            mass_plans = explain(project, args.output, "mass_pass") if args.explain_mass_pass else {}
            mass_pass = project.reconcile_seconds()
            archived = project.sql(
                "select count(*) from @schema.dqm_issue_occurrences "
                "where test_name='scale_synthetic' and record_status='Active'"
            )[0][0]
            server = project.sql("show server_version")[0][0]
        finally:
            if not args.keep:
                project.sql(f'drop schema if exists "{schema}" cascade')
                project.sql(f'drop schema if exists "{schema}_dbt_test__audit" cascade')
    report = {
        "adapter": "postgres",
        "server_version": server,
        "volumes": {
            "tests": args.tests,
            "occurrences": args.occurrences,
            "active_occurrences": int(args.occurrences * args.active_fraction),
            "executions": args.executions,
            "observations": args.observations,
        },
        "synthetic_load_seconds": round(load_seconds, 1),
        "steady_reconcile_seconds": round(steady, 2),
        "steady_plan_execution_seconds": plans,
        "mass_pass_reconcile_seconds": round(mass_pass, 2),
        "mass_pass_plan_execution_seconds": mass_plans,
        "active_scale_occurrences_after_mass_pass": archived,
        "app_sync": sync,
    }
    (args.output / "postgres.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
