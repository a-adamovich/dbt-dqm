"""Measure warehouse browsing in a fresh process: latency per read, bytes, plans and peak RSS.

    uv run python integration_tests/scale/browse_probe.py \\
      --project-dir PATH --profiles-dir PATH --target dev --output DIR [--repeat 5]

It calls the app's own read functions (`warehouse.browse_options`, `browse_summary`,
`browse_page`), each with its generation bracketing, for the whole Active population and for a
set of typical filters, and stores each page in a temporary page cache as the app does. Times are
wall-clock per call (median and max over `--repeat` calls). On BigQuery it also records the
bytes processed and billed by every job; on Postgres it writes `EXPLAIN (ANALYZE, BUFFERS)` for
the first page, the next page, a status filter and a search. Peak RSS covers the whole process.
"""

from __future__ import annotations

import argparse
import json
import resource
import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from dbt_dqm_app import browse, warehouse
from dbt_dqm_app.browse import Filters
from dbt_dqm_app.config import load_config
from dbt_dqm_app.store import Workspace

TODAY = datetime.now().astimezone().date()
CASES = {
    "all_active": Filters(),
    "status_blocked": Filters(statuses=("BLOCKED",)),
    "priority_high": Filters(priorities=("high",)),
    "owner": Filters(owners=("owner_03",)),
    "created_last_30_days": Filters(created_from=TODAY - timedelta(days=30), created_to=TODAY),
    "tag": Filters(tags=("finance",)),
    "recurrences_only": Filters(recurrences_only=True),
    "search": Filters(search="c-4242"),
    "search_with_status": Filters(search="c-4242", statuses=("NEW",)),
}


def peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / 2**20 if sys.platform == "darwin" else peak / 2**10, 1)


class BigQueryJobs:
    """Record bytes for every BigQuery job the probe runs (dry runs are reported separately)."""

    def __init__(self):
        from google.cloud import bigquery

        self.jobs: list[dict] = []
        original = bigquery.Client.query

        def query(client, sql, *args, **kwargs):
            job = original(client, sql, *args, **kwargs)
            config = kwargs.get("job_config")
            if config is not None and config.dry_run:
                self.jobs.append({"dry_run": True, "processed": job.total_bytes_processed})
                return job
            result = job.result

            def finished(*result_args, **result_kwargs):
                rows = result(*result_args, **result_kwargs)
                self.jobs.append({"dry_run": False, "cache_hit": job.cache_hit,
                                  "processed": job.total_bytes_processed,
                                  "billed": job.total_bytes_billed})
                return rows

            job.result = finished
            return job

        bigquery.Client.query = query

    def take(self) -> list[dict]:
        jobs, self.jobs = self.jobs, []
        return jobs


JOBS: BigQueryJobs | None = None


def timed(read, repeat):
    """Median and max wall time; on BigQuery also the jobs of the first, uncached call (later
    calls of the same read are answered from BigQuery's query cache, as in the app)."""
    seconds, result, first_jobs = [], None, None
    for index in range(repeat):
        started = time.perf_counter()
        result = read()
        seconds.append(time.perf_counter() - started)
        if JOBS is not None and index == 0:
            first_jobs = JOBS.take()
    measured = {"median_s": round(statistics.median(seconds), 3), "max_s": round(max(seconds), 3),
                "first_s": round(seconds[0], 3)}
    if JOBS is not None:
        JOBS.take()
        measured["first_call_jobs"] = first_jobs
        measured["first_call_mb_processed"] = round(
            sum((job.get("processed") or 0) for job in first_jobs if not job["dry_run"]) / 1e6, 1)
        measured["first_call_mb_billed"] = round(
            sum((job.get("billed") or 0) for job in first_jobs if not job["dry_run"]) / 1e6, 1)
    return result, measured


def explain_postgres(config, population, output: Path, label: str) -> dict[str, float]:
    relation = warehouse._table(config, "dqm_all_issues")
    first = warehouse.browse_page(config, population, Filters(), None, 25)
    queries = {
        "first_page": browse.page_query("postgres", relation, population, Filters(), None, 25),
        "next_page": browse.page_query("postgres", relation, population, Filters(),
                                       first.next_cursor, 25),
        "status_blocked": browse.page_query("postgres", relation, population,
                                            CASES["status_blocked"], None, 25),
        "search": browse.page_query("postgres", relation, population, CASES["search"], None, 25),
        "count_all": browse.count_query("postgres", relation, population, Filters()),
        "metrics": browse.metrics_query("postgres", relation, population),
    }
    times = {}
    with warehouse._postgres_connection(config) as connection, connection.cursor() as cursor:
        for name, query in queries.items():
            cursor.execute("explain (analyze, buffers) " + query.sql, query.params)
            plan = "\n".join(row[0] for row in cursor.fetchall())
            (output / f"postgres-browse-plan-{label}-{name}.txt").write_text(plan + "\n")
            line = next(text for text in plan.splitlines() if "Execution Time" in text)
            times[name] = float(line.split(":")[1].split()[0]) / 1000
    return times


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--profiles-dir", required=True)
    parser.add_argument("--target", default="dev")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--deep-pages", type=int, default=10)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    baseline = peak_rss_mb()
    config = load_config(args.project_dir, args.profiles_dir, args.target)
    global JOBS
    jobs = JOBS = BigQueryJobs() if config.adapter_type == "bigquery" else None
    population = warehouse.browse_population(config)
    report: dict = {"adapter": config.adapter_type, "label": args.label, "cases": {}}

    _, report["options"] = timed(lambda: warehouse.browse_options(config, population), 1)
    with tempfile.TemporaryDirectory() as directory:
        workspace = Workspace(Path(directory) / "workspace.sqlite")
        for name, filters in CASES.items():
            case: dict = {}
            summary, case["summary"] = timed(
                lambda filters=filters: warehouse.browse_summary(config, population, filters),
                args.repeat)
            case["matching"] = summary["matching"]
            page, case["first_page"] = timed(
                lambda filters=filters: warehouse.browse_page(config, population, filters, None, 25),
                args.repeat)
            workspace.upsert_pages(page.rows)
            if page.next_cursor is not None:
                second, case["next_page"] = timed(
                    lambda filters=filters, cursor=page.next_cursor: warehouse.browse_page(
                        config, population, filters, cursor, 25),
                    args.repeat)
                workspace.upsert_pages(second.rows)
            report["cases"][name] = case
        deep, cursor = [], None
        for _ in range(args.deep_pages):
            started = time.perf_counter()
            page = warehouse.browse_page(config, population, Filters(), cursor, 200)
            deep.append(round(time.perf_counter() - started, 3))
            workspace.upsert_pages(page.rows)
            cursor = page.next_cursor
            if cursor is None:
                break
        report["deep_paging_200_per_page_s"] = deep
        if jobs:
            jobs.take()
        report["page_cache_rows"] = workspace.cache_size()[0]
    if config.adapter_type == "postgres":
        report["postgres_plan_execution_s"] = explain_postgres(
            config, population, args.output, args.label)
    report["baseline_rss_mb"] = baseline
    report["peak_rss_mb"] = peak_rss_mb()
    print(json.dumps(report, default=str))


if __name__ == "__main__":
    main()
