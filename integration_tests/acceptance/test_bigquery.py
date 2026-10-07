"""Credentialed release gate; uses new disposable datasets, never consumer tables.

Set DBT_DQM_BIGQUERY_TEST_PROJECT and ADC (GOOGLE_APPLICATION_CREDENTIALS).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from google.cloud import bigquery

ROOT = Path(__file__).resolve().parents[2]
PROJECT = os.environ.get("DBT_DQM_BIGQUERY_TEST_PROJECT")
pytestmark = pytest.mark.skipif(not PROJECT, reason="Credentialed BigQuery release gate")


class Demo:
    def __init__(self, path, profiles, dataset, client):
        self.path, self.profiles, self.dataset, self.client = path, profiles, dataset, client
        self.has_captured = False

    def dbt(self, *args, variables=None, success=True, target="target"):
        command = [
            str(Path(sys.executable).with_name("dbt")),
            *args,
            "--project-dir",
            str(self.path),
            "--profiles-dir",
            str(self.profiles),
            "--target-path",
            target,
            "--log-path",
            str(self.path / ("logs-" + target)),
        ]
        if variables:
            command += ["--vars", json.dumps(variables)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
        if success:
            assert result.returncode == 0, result.stdout + result.stderr
        else:
            assert result.returncode != 0, result.stdout
        return json.loads((self.path / target / "run_results.json").read_text())["metadata"][
            "invocation_id"
        ]

    def sql(self, sql):
        sql = sql.replace("@dataset", f"`{PROJECT}.{self.dataset}")
        return [dict(row.items()) for row in self.client.query(sql).result()]

    def capture(self, scenario, **extra):
        variables = {"demo_scenario": scenario, **extra}
        self.dbt("run", "--select", "demo_records", variables=variables)
        selection = [] if not self.has_captured else ["--exclude", "tag:guardrail"]
        self.dbt("test", "--select", "tag:dqm", *selection, variables=variables)
        self.has_captured = True

    def snapshot(self):
        return self.sql("select * from @dataset.dqm_issue_occurrences` order by occurrence_id")

    def freeze(self, target="target"):
        return self.dbt("run-operation", "acceptance_freeze", target=target)

    def apply(self, run_id, fault=False, success=True, target="target"):
        return self.dbt(
            "run-operation",
            "acceptance_apply",
            "--args",
            json.dumps({"run_id": run_id, "fault": fault}),
            success=success,
            target=target,
        )


@pytest.fixture
def demo(tmp_path):
    dataset = "dqm_acceptance_" + uuid.uuid4().hex[:12]
    location = os.environ.get("DBT_DQM_BIGQUERY_TEST_LOCATION", "US")
    client = bigquery.Client(project=PROJECT, location=location)
    value = bigquery.Dataset(f"{PROJECT}.{dataset}")
    value.location = location
    client.create_dataset(value)
    path = tmp_path / "project"
    shutil.copytree(
        ROOT / "integration_tests/demo_bigquery",
        path,
        ignore=shutil.ignore_patterns("target", "logs", "dbt_packages"),
    )
    (path / "dbt_packages").mkdir()
    (path / "dbt_packages/dbt_dqm").symlink_to(ROOT, target_is_directory=True)
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "profiles.yml").write_text(
        yaml.safe_dump(
            {
                "dbt_dqm_demo": {
                    "target": "dev",
                    "outputs": {
                        "dev": {
                            "type": "bigquery",
                            "method": "oauth",
                            "project": PROJECT,
                            "dataset": dataset,
                            "location": location,
                            "threads": 4,
                        }
                    },
                }
            }
        )
    )
    # Sorts before dbt_dqm, so its on-run-end hook runs first and can change a stored-failure
    # table after dbt computed result.failures, as a concurrent invocation would.
    tamper = path / "dbt_packages/aaa_acceptance_tamper"
    (tamper / "macros").mkdir(parents=True)
    (tamper / "dbt_project.yml").write_text(
        yaml.safe_dump(
            {
                "name": "aaa_acceptance_tamper",
                "version": "1.0.0",
                "config-version": 2,
                "macro-paths": ["macros"],
                "on-run-end": ["{{ aaa_acceptance_tamper.tamper_failures() }}"],
            }
        )
    )
    (tamper / "macros/tamper.sql").write_text("""
{% macro tamper_failures() %}{% if execute and var('tamper', false) %}
insert into `{{ target.project }}.{{ target.schema }}.demo_customer_email_invalid` (customer_id, email, reason)
values ('tamper-row', 'tamper@example.com', 'tampered');
{% endif %}{% endmacro %}
""")
    result = Demo(path, profiles, dataset, client)
    try:
        result.dbt("seed")
        yield result
    finally:
        for name in (dataset, dataset + "_dbt_test__audit"):
            client.delete_dataset(f"{PROJECT}.{name}", delete_contents=True, not_found_ok=True)


def test_bigquery_batch_rollback_lost_response_and_empty(demo):
    for scenario in ("initial", "passed", "recurrence"):
        demo.capture(scenario)
    frozen = demo.freeze()
    demo.apply(frozen, fault=True, success=False)
    assert not demo.snapshot()
    assert not demo.sql("select * from @dataset.dqm_reconciliation_receipts`")
    frozen = demo.freeze()
    demo.apply(frozen)
    before = demo.snapshot()
    assert len(before) == 10
    events = demo.sql("select * from @dataset.dqm_issue_events` order by event_id")
    # Discarding a successful response is equivalent to a caller retrying a committed operation.
    demo.dbt("run", "--select", "dqm_reconcile")
    assert demo.snapshot() == before
    assert demo.sql("select * from @dataset.dqm_issue_events` order by event_id") == events
    demo.dbt("build", "--select", "package:dbt_dqm", variables={"demo_scenario": "recurrence"})
    generation = demo.sql("select generation from @dataset.dqm_reconciliation_control`")
    for command in ("run", "build"):
        demo.dbt(
            command,
            "--empty",
            "--select",
            "package:dbt_dqm",
            variables={"demo_scenario": "recurrence"},
        )
        assert demo.snapshot() == before
        assert demo.sql("select generation from @dataset.dqm_reconciliation_control`") == generation
        # --empty disables tracking writes only; the public views keep serving real data.
        assert demo.sql("select count(*) n from @dataset.dqm_all_issues`")[0]["n"] == len(before)


def test_bigquery_stale_generation_annotations_and_abandoned_runner(demo):
    demo.capture("initial")
    demo.dbt("build", "--select", "package:dbt_dqm")
    demo.capture("passed")
    frozen = demo.freeze()
    # This edit commits after staging. Apply must use the target workflow and preserve annotations.
    demo.sql(
        "update @dataset.dqm_issue_occurrences` set notes='concurrent edit',workflow_status='RESOLVED',annotation_version=annotation_version+1 where true"
    )
    demo.apply(frozen)
    assert all(
        row["workflow_status_at_close"] == "RESOLVED"
        and row["notes"] == "concurrent edit"
        and row["annotation_version"] == 1
        for row in demo.snapshot()
    )
    demo.capture("recurrence")
    first = demo.freeze()
    demo.sql(
        "update @dataset.dqm_reconciliation_runs` set started_at=timestamp_sub(current_timestamp(),interval 2 hour) where status='started'"
    )
    second = demo.freeze()
    demo.apply(first)
    before = demo.snapshot()
    demo.apply(second, success=False)
    assert demo.snapshot() == before
    abandoned = demo.freeze()
    demo.sql(
        "update @dataset.dqm_reconciliation_runs` set started_at=timestamp_sub(current_timestamp(),interval 2 hour) where status='started'"
    )
    demo.dbt("run-operation", "dqm_abandon_runs")
    demo.apply(abandoned, success=False)
    assert demo.snapshot() == before
    demo.dbt("run", "--select", "dqm_reconcile")
    assert len(demo.sql("select * from @dataset.dqm_all_issues`")) == 10


def test_bigquery_overlapping_applies(demo):
    demo.capture("initial")
    first = demo.freeze()
    demo.sql(
        "update @dataset.dqm_reconciliation_runs` set started_at=timestamp_sub(current_timestamp(),interval 2 hour) where status='started'"
    )
    second = demo.freeze()
    with ThreadPoolExecutor(2) as pool:
        tasks = [
            pool.submit(demo.apply, run, target=f"target-{i}")
            for i, run in enumerate((first, second))
        ]
        successes = 0
        for task in tasks:
            try:
                task.result()
                successes += 1
            except AssertionError:
                pass
    assert successes <= 1
    rows = demo.snapshot()
    assert len({row["occurrence_id"] for row in rows}) == len(rows)
    assert len(rows) in (0, 7)
    # Interrupted clients are fenced before a clean retry, even if both competing jobs aborted.
    demo.sql(
        "update @dataset.dqm_reconciliation_runs` set started_at=timestamp_sub(current_timestamp(),interval 2 hour) where status='started'"
    )
    demo.dbt("run-operation", "dqm_abandon_runs")
    demo.dbt("run", "--select", "dqm_reconcile")
    assert len(demo.snapshot()) == 7


def test_bigquery_concurrent_install_and_interrupted_migration(demo):
    with ThreadPoolExecutor(2) as pool:
        tasks = [
            pool.submit(demo.dbt, "run-operation", "acceptance_setup", target=f"target-init-{i}")
            for i in range(2)
        ]
        for task in tasks:
            try:
                task.result()
            except AssertionError:
                pass  # Competing initialization can fail its ownership claim; rerun idempotently.
    demo.dbt("run-operation", "acceptance_setup")
    assert demo.sql("select count(*) n from @dataset.dqm_install`")[0]["n"] == 1
    assert demo.sql("select count(*) n from @dataset.dqm_reconciliation_control`")[0]["n"] == 1
    demo.capture("initial")
    demo.dbt("build", "--select", "package:dbt_dqm")
    before = demo.snapshot()
    demo.dbt(
        "run-operation",
        "acceptance_setup",
        variables={"acceptance_future": True, "acceptance_interrupt": True},
        success=False,
    )
    owner = demo.sql("select migration_owner from @dataset.dqm_reconciliation_control`")[0][
        "migration_owner"
    ]
    assert owner
    demo.sql(
        "update @dataset.dqm_reconciliation_control` set migration_lease_until=timestamp_sub(current_timestamp(),interval 1 minute) where true"
    )
    demo.dbt("run-operation", "acceptance_setup", variables={"acceptance_future": True})
    after = demo.snapshot()
    assert [
        {k: v for k, v in row.items() if k != "acceptance_future_field"} for row in after
    ] == before
    assert all(row["acceptance_future_field"] == "preserved" for row in after)
    control = demo.sql("select * from @dataset.dqm_reconciliation_control`")[0]
    assert control["schema_version"] == "0002_acceptance" and control["migration_owner"] is None


def test_bigquery_skip_invalidates_frozen_inputs(demo):
    demo.capture("initial")
    demo.dbt("build", "--select", "package:dbt_dqm")
    frozen = demo.freeze()
    ids = demo.sql(
        "select test_unique_id from @dataset.dqm_reconciliation_state` order by test_unique_id limit 2"
    )
    for row in ids:
        test = row["test_unique_id"]
        demo.sql(
            f"insert into @dataset.dqm_test_executions` (invocation_id,captured_at,test_unique_id,test_status,collection_status) values ('shared-late',timestamp_sub(current_timestamp(),interval 1 day),'{test}','pass','not_applicable')"
        )
    items = [
        {"test_unique_id": row["test_unique_id"], "invocation_id": "shared-late"} for row in ids
    ]
    before = demo.snapshot()
    demo.dbt(
        "run-operation",
        "dqm_skip_late_evidence",
        "--args",
        json.dumps({"items": items, "reason": "Reviewed delayed capture"}),
    )
    demo.apply(frozen, success=False)
    assert demo.snapshot() == before
    gaps = demo.sql(
        "select event_id from @dataset.dqm_issue_events` where event_type='EVIDENCE_SKIPPED'"
    )
    assert len(gaps) == 2 and len({r["event_id"] for r in gaps}) == 2
    demo.dbt("run", "--select", "dqm_reconcile")
    assert demo.snapshot() == before


def test_bigquery_app_audit_missed_retry_and_health(demo):
    from dbt_dqm_app.config import load_config
    from dbt_dqm_app.store import Patch
    from dbt_dqm_app.warehouse import apply_patches, fetch_health, insert_missed_issue

    principal = "serviceAccount:" + demo.client._credentials.service_account_email
    project_file = demo.path / "dbt_project.yml"
    project = yaml.safe_load(project_file.read_text())
    grants = {"roles/bigquery.dataViewer": [principal]}
    project.setdefault("vars", {})["dbt_dqm_table_grants"] = {
        "dqm_issue_occurrences": grants,
        "dqm_annotation_changes": grants,
        "dqm_missed_issues": grants,
    }
    project.setdefault("models", {}).setdefault("dbt_dqm", {}).update(
        {"+grants": grants, "+persist_docs": {"relation": True, "columns": True}}
    )
    project_file.write_text(yaml.safe_dump(project))
    demo.capture("initial")
    demo.dbt("build", "--select", "package:dbt_dqm")
    for name in [
        "dqm_issue_occurrences",
        "dqm_annotation_changes",
        "dqm_missed_issues",
        "dqm_reconcile",
        "dqm_all_issues",
        "dqm_test_health",
    ]:
        policy = demo.client.get_iam_policy(f"{PROJECT}.{demo.dataset}.{name}")
        assert any(
            binding["role"] == "roles/bigquery.dataViewer" and principal in binding["members"]
            for binding in policy.bindings
        ), name
    config = load_config(demo.path, demo.profiles, "dev")
    row = demo.snapshot()[0]
    patch = Patch(
        row["occurrence_id"],
        "review_verdict",
        "UNREVIEWED",
        "TRUE_POSITIVE",
        1,
        datetime.now(UTC).isoformat(),
        row["annotation_version"],
    )
    batch = apply_patches(config, [patch])
    assert apply_patches(config, [patch]) == batch
    audit = demo.sql(
        "select * from @dataset.dqm_annotation_changes` where field_name='review_verdict'"
    )
    assert len(audit) == 1
    assert audit[0]["resulting_annotation_version"] == row["annotation_version"] + 1
    report = {
        "missed_issue_id": str(uuid.uuid4()),
        "area_label": "Unmapped acceptance area",
        "description": "Synthetic confirmed escape",
        "root_cause": "no_test",
        "discovered_at": datetime.now(UTC).isoformat(),
    }
    insert_missed_issue(config, report)
    insert_missed_issue(config, report)
    assert demo.sql("select count(*) n from @dataset.dqm_missed_issues`")[0]["n"] == 1
    health = fetch_health(config)
    assert health["tests"] and health["areas"]
    assert sum(item["assessed_count"] for item in health["tests"]) == 1
    assert sum(item["known_missed_issue_count"] for item in health["areas"]) == 1


def test_bigquery_retention_keeps_unprocessed_evidence_and_durable_events(demo):
    demo.capture("initial")
    demo.dbt("build", "--select", "package:dbt_dqm")
    before = demo.snapshot()
    events = demo.sql("select * from @dataset.dqm_issue_events` order by event_id")
    state = demo.sql("select * from @dataset.dqm_reconciliation_state` order by test_unique_id")
    unprocessed = demo.sql(
        "select execution.test_unique_id,execution.invocation_id from @dataset.dqm_test_executions` execution where not exists(select 1 from @dataset.dqm_reconciliation_receipts` receipt where receipt.test_unique_id=execution.test_unique_id and receipt.invocation_id=execution.invocation_id)"
    )
    demo.sql(
        "update @dataset.dqm_test_executions` set captured_at=timestamp_sub(current_timestamp(),interval 45 day) where true"
    )
    demo.sql(
        "insert into @dataset.dqm_test_executions` (invocation_id,captured_at,test_unique_id,test_status,collection_status) values ('unprocessed',timestamp_sub(current_timestamp(),interval 45 day),'untracked-retention-fixture','pass','not_applicable')"
    )
    demo.dbt("run-operation", "cleanup_dqm_logs", "--args", '{"retention_days":30}')
    remaining = demo.sql("select test_unique_id,invocation_id from @dataset.dqm_test_executions`")
    expected = unprocessed + [
        {"test_unique_id": "untracked-retention-fixture", "invocation_id": "unprocessed"}
    ]
    assert sorted(
        remaining, key=lambda row: (row["test_unique_id"], row["invocation_id"])
    ) == sorted(expected, key=lambda row: (row["test_unique_id"], row["invocation_id"]))
    assert not demo.sql("select * from @dataset.dqm_issue_observations`")
    assert not demo.sql("select * from @dataset.dqm_reconciliation_receipts`")
    assert demo.sql("select * from @dataset.dqm_issue_events` order by event_id") == events
    assert (
        demo.sql("select * from @dataset.dqm_reconciliation_state` order by test_unique_id")
        == state
    )
    demo.dbt("run", "--select", "dqm_reconcile")
    assert demo.snapshot() == before


def test_bigquery_uninitialized_empty_and_legacy_rejection_have_no_marker(demo):
    demo.dbt("run", "--empty", "--select", "dqm_reconcile", success=False)
    assert not demo.sql(
        "select table_name from @dataset.INFORMATION_SCHEMA.TABLES` where table_name like 'dqm_%'"
    )
    demo.sql("create table @dataset.dqm_annotation_changes` (legacy_marker string)")
    demo.dbt("run-operation", "acceptance_setup", success=False)
    names = demo.sql(
        "select table_name from @dataset.INFORMATION_SCHEMA.TABLES` where table_name like 'dqm_%'"
    )
    assert names == [{"table_name": "dqm_annotation_changes"}]


def test_bigquery_completed_stage_cleanup_waits_for_retention(demo):
    from google.api_core.exceptions import NotFound

    run = demo.freeze()
    demo.apply(run)
    stage = demo.sql(
        f"select stage_relation,completed_at from @dataset.dqm_reconciliation_runs` where run_id='{run}'"
    )[0]
    name = stage["stage_relation"].replace("`", "")
    table = demo.client.get_table(name)
    assert (table.expires - stage["completed_at"]).total_seconds() >= 24 * 60 * 60
    demo.dbt("run-operation", "dqm_cleanup_stages")
    assert demo.client.get_table(name)
    demo.sql(
        f"update @dataset.dqm_reconciliation_runs` set completed_at=timestamp_sub(current_timestamp(),interval 25 hour) where run_id='{run}'"
    )
    demo.dbt("run-operation", "dqm_cleanup_stages")
    with pytest.raises(NotFound):
        demo.client.get_table(name)


def _latest_execution(demo, test_name):
    return demo.sql(
        "select * from @dataset.dqm_test_executions` "
        f"where test_name='{test_name}' order by captured_at desc limit 1"
    )[0]


def test_bigquery_capture_rejects_failure_tables_changed_before_capture(demo):
    customer = "demo_customer_email_invalid"
    demo.capture("initial")
    demo.dbt("run", "--select", "dqm_reconcile")
    baseline = [row for row in demo.snapshot() if row["test_name"] == customer]
    assert baseline and all(row["record_status"] == "Active" for row in baseline)

    demo.capture("initial", tamper=True)
    tampered = _latest_execution(demo, customer)
    assert tampered["collection_status"] == "collection_error"
    assert "Stored failures changed before capture" in tampered["collection_message"]
    assert not demo.sql(
        "select 1 from @dataset.dqm_issue_observations` "
        f"where invocation_id='{tampered['invocation_id']}' "
        f"and test_unique_id='{tampered['test_unique_id']}'"
    )
    assert _latest_execution(demo, "demo_order_amount_invalid")["collection_status"] == "success"

    demo.capture("passed", tamper=True)
    tampered_pass = _latest_execution(demo, customer)
    assert tampered_pass["collection_status"] == "collection_error"
    assert "dbt reported 0 rows, the table had 1" in tampered_pass["collection_message"]
    assert (
        _latest_execution(demo, "demo_order_amount_invalid")["collection_status"]
        == "not_applicable"
    )
    demo.dbt("run", "--select", "dqm_reconcile")
    rows = demo.snapshot()
    assert all(r["record_status"] == "Active" for r in rows if r["test_name"] == customer)
    assert all(
        r["record_status"] == "Archived" and r["close_reason"] == "Passed"
        for r in rows
        if r["test_name"] != customer
    )
