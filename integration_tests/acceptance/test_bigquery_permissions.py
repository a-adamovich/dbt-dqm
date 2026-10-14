"""Opt-in, serial permission gate on the pre-created QA dataset after authorized reset.

Set DBT_DQM_BIGQUERY_RUNNER_CREDENTIALS and DBT_DQM_BIGQUERY_REVIEWER_CREDENTIALS to
separate local ADC files. The existing destructive BigQuery fixture is deliberately not reused.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from google.api_core.exceptions import Forbidden
from google.cloud import bigquery
from google.oauth2 import service_account

from dbt_dqm_app.browse import Filters
from dbt_dqm_app.config import load_config
from dbt_dqm_app.exports import prepare_export
from dbt_dqm_app.store import Patch
from dbt_dqm_app.warehouse import (
    apply_patches,
    browse_page,
    browse_population,
    browse_summary,
    fetch_issues,
    insert_missed_issue,
    sync_worker,
)

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "dbt-dqm"
DATASET = "dbt_dqm_qa"
RUNNER = "dbt-dqm-runner@dbt-dqm.iam.gserviceaccount.com"
REVIEWER = "dbt-dqm-reviewer@dbt-dqm.iam.gserviceaccount.com"
RUNNER_FILE = os.environ.get("DBT_DQM_BIGQUERY_RUNNER_CREDENTIALS")
REVIEWER_FILE = os.environ.get("DBT_DQM_BIGQUERY_REVIEWER_CREDENTIALS")
pytestmark = pytest.mark.skipif(
    not (RUNNER_FILE and REVIEWER_FILE), reason="Separate restricted-account ADC files required"
)


def _client(path):
    # The gate's runner and reviewer identities are service accounts, so load only that type.
    credentials = service_account.Credentials.from_service_account_file(
        path, scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    return bigquery.Client(project=PROJECT, credentials=credentials, location="US")


def test_restricted_bigquery_runner_and_reviewer(tmp_path):
    """Run alone: requires exclusive use of QA, leaves uniquely named synthetic fixtures.

    No administrative client is used. In particular, an unexpectedly successful denied probe
    fails this gate rather than silently fixing privileges or deleting tables.
    """
    runner, reviewer = _client(RUNNER_FILE), _client(REVIEWER_FILE)
    for client, expected in ((runner, RUNNER), (reviewer, REVIEWER)):
        identity = next(iter(client.query("select session_user() as identity").result())).identity
        assert identity == expected, f"Refusing elevated/wrong identity: {identity}"
        assert client.get_dataset(f"{PROJECT}.{DATASET}").location == "US"

    token = uuid.uuid4().hex[:16]
    fixture = "qa_permissions_" + token
    path = tmp_path / "project"
    shutil.copytree(
        ROOT / "integration_tests/demo_bigquery", path,
        ignore=shutil.ignore_patterns("target", "logs", "dbt_packages"),
    )
    (path / "dbt_packages").mkdir()
    (path / "dbt_packages/dbt_dqm").symlink_to(ROOT, target_is_directory=True)
    (path / f"seeds/{fixture}.csv").write_text("id,value\n" + token + ",invalid\n")
    (path / f"models/{fixture}_records.sql").write_text(
        "select * from {{ ref('" + fixture + "') }}"
    )
    test_name = fixture + "_invalid"
    (path / f"tests/{test_name}.sql").write_text(
        "{{ config(tags=['dqm'], store_failures=true, "
        "meta={'dbt_dqm': {'granularity': ['id']}}) }}\n"
        "select * from {{ ref('" + fixture + "_records') }} where value='invalid'"
    )
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "profiles.yml").write_text(yaml.safe_dump({"dbt_dqm_demo": {
        "target": "qa", "outputs": {"qa": {"type": "bigquery", "method": "oauth",
            "project": PROJECT, "dataset": DATASET, "location": "US", "threads": 1}},
    }}))
    environment = {**os.environ, "GOOGLE_APPLICATION_CREDENTIALS": str(Path(RUNNER_FILE).resolve())}

    def dbt(*args, test_failure=False, dqm=False):
        result = subprocess.run(
            [str(Path(sys.executable).with_name("dbt-dqm" if dqm else "dbt")), *args,
             "--project-dir", str(path), "--profiles-dir", str(profiles)],
            env=environment, capture_output=True, text=True, timeout=600, check=False,
        )
        assert result.returncode == (1 if test_failure else 0), result.stdout + result.stderr
        if test_failure:
            results = [r for r in json.loads((path / ("target/dqm_run_results.json" if dqm else "target/run_results.json")).read_text())["results"]
                       if r["unique_id"].startswith("test.")]
            assert len(results) == 1 and results[0]["status"] == "fail"

    installed = list(runner.query(f"select marker from `{PROJECT}.{DATASET}.dqm_install`").result())
    assert len(installed) == 1 and installed[0].marker == "dqm-full-v1", (
        "Prepare the authorized scoped QA full refresh before running this restricted gate")
    migrations = {r.id for r in runner.query(
        f"select migration_id as id from `{PROJECT}.{DATASET}.dqm_schema_migrations`"
    ).result()}
    assert "0003_app_change_staging" in migrations
    assert "0004_app_staging_safety" in migrations
    assert "0005_maintenance_log" in migrations
    assert "0007_browse_keyset_index" in migrations

    dbt("seed", "--select", fixture)
    dbt("run", "--select", fixture + "_records")
    dbt("run", "--select", test_name, test_failure=True, dqm=True)
    # A ten-year retention schedules raw and run pruning without deleting shared QA history; run
    # pruning reads the dataset's INFORMATION_SCHEMA.TABLES as the runner.
    dbt("run", "--select", test_name, "--vars", '{"dbt_dqm_retention_days": 3650}', test_failure=True, dqm=True)
    invocation = json.loads((path / "target/dqm_model_run_results.json").read_text())["metadata"]["invocation_id"]
    pruning = {r.step: r.outcome for r in runner.query(
        f"select step, outcome from `{PROJECT}.{DATASET}.dqm_maintenance_log` "
        f"where invocation_id='{invocation}'"
    ).result()}
    assert pruning.get("run_pruning") == "succeeded" and pruning.get("raw_pruning") == "succeeded"
    dbt("test", "--select", "package:dbt_dqm", "--exclude", "assert_scenario_outcomes",
        "--vars", "{dqm_enabled: true}")

    config = replace(load_config(path, profiles, "qa"), credentials_file=Path(REVIEWER_FILE))
    snapshot = sync_worker(config)  # Includes reviewer-authenticated dbt debug/parse.
    rows = snapshot["issues"]
    owned = [r for r in rows if r["test_unique_id"].endswith("." + test_name)]
    assert len(owned) == 1
    row = owned[0]
    assert "tests" in snapshot["health"]
    # Reviewers read maintenance health (the view reads dqm_maintenance_log with their access).
    maintenance = snapshot["health"]["maintenance"]
    assert maintenance is not None and {m["step"] for m in maintenance} >= {"staging_expiry"}
    patches = [Patch(row["occurrence_id"], field, row.get(field), value, 1,
                     datetime.now(UTC).isoformat(), row["annotation_version"], installation_id=row["_installation_id"])
               for field, value in (("notes", "Permissions QA " + token),
                                    ("review_verdict", "TRUE_POSITIVE"))]
    batch = apply_patches(config, patches)
    assert apply_patches(config, patches) == batch
    refreshed = next(r for r in fetch_issues(config) if r["occurrence_id"] == row["occurrence_id"])
    assert refreshed["annotation_version"] == row["annotation_version"] + 1
    assert refreshed["notes"] == "Permissions QA " + token
    assert refreshed["review_verdict"] == "TRUE_POSITIVE"
    # Warehouse browsing with reviewer access, including the search dry run.
    population = browse_population(config)
    found = browse_page(config, population, Filters(lifecycle=(), search="Permissions QA " + token),
                        None, 25)
    assert [r["occurrence_id"] for r in found.rows] == [row["occurrence_id"]]
    summary = browse_summary(config, population, Filters(search=token))
    assert summary["matching"] == 1
    # Complete exports also use the restricted reviewer identity and leave the audit unchanged.
    for format in ("csv", "xlsx"):
        exported = prepare_export(config, tmp_path / "unused.sqlite",
            Filters(lifecycle=(), search="Permissions QA " + token), tmp_path / ("export-" + format),
            format, population, summary["generation"])
        assert exported.rows == 1
        if format == "csv":
            with open(exported.path, encoding="utf-8-sig", newline="") as file:
                exported_row, = csv.DictReader(file)
            assert exported_row["occurrence_id"] == row["occurrence_id"]
            assert exported_row["notes"] == "Permissions QA " + token
            assert exported_row["review_verdict"] == "TRUE_POSITIVE"
    count = reviewer.query(
        f"select count(*) as n from `{PROJECT}.{DATASET}.dqm_annotation_changes` "
        "where batch_id=@batch",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("batch", "STRING", batch)]),
    ).result()
    assert next(iter(count)).n == 2
    report = {"missed_issue_id": str(uuid.uuid4()), "area_label": "Permissions QA",
              "description": "Synthetic permissions check " + token, "root_cause": "no_test"}
    assert insert_missed_issue(config, report) == insert_missed_issue(config, report)
    report_count = reviewer.query(
        f"select count(*) as n from `{PROJECT}.{DATASET}.dqm_missed_issues` where missed_issue_id=@id",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("id", "STRING", report["missed_issue_id"])]),
    ).result()
    assert next(iter(report_count)).n == 1
    for table, column in (("dqm_reconciliation_control", "generation"),
                          ("dqm_test_executions", "invocation_id"),
                          ("dqm_reconciliation_receipts", "invocation_id"),
                          ("dqm_maintenance_log", "step")):
        with pytest.raises(Forbidden):
            reviewer.query(
                f"update `{PROJECT}.{DATASET}.{table}` set {column}={column} where false"
            ).result()
    with pytest.raises(Forbidden):
        reviewer.query(f"create table `{PROJECT}.{DATASET}.{fixture}_denied` (id string)").result()
