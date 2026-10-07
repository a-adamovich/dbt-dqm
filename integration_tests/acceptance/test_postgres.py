"""Warehouse acceptance tests. Set DBT_DQM_TEST_DSN to a disposable PostgreSQL database.

Every test creates its own schema and removes that schema afterward. No consumer data is read.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import psycopg2
import pytest
import yaml
from psycopg2.extensions import parse_dsn
from psycopg2.extras import RealDictCursor

ROOT = Path(__file__).resolve().parents[2]
DSN = os.environ.get("DBT_DQM_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="Set DBT_DQM_TEST_DSN for disposable warehouse tests"
)


class Demo:
    def __init__(self, project, profiles, schema):
        self.project, self.profiles, self.schema = project, profiles, schema

    def dbt(self, *args, variables=None, success=True):
        command = [
            str(Path(sys.executable).with_name("dbt")),
            *args,
            "--project-dir",
            str(self.project),
            "--profiles-dir",
            str(self.profiles),
        ]
        if variables:
            command += ["--vars", json.dumps(variables)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=90, check=False)
        (self.project / "latest-command.log").write_text(result.stdout + result.stderr)
        if success:
            assert result.returncode == 0, result.stdout + result.stderr
        else:
            assert result.returncode != 0, result.stdout
        return result

    def sql(self, statement, params=None):
        with psycopg2.connect(DSN) as conn, conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(statement.replace("@schema", f'"{self.schema}"'), params)
            return [dict(row) for row in cursor.fetchall()] if cursor.description else []

    def capture(self, scenario="initial", **variables):
        variables["demo_scenario"] = scenario
        self.dbt("run", "--select", "demo_records", variables=variables)
        self.dbt("test", "--select", "tag:dqm", variables=variables)

    def occurrences(self):
        return self.sql("select * from @schema.dqm_issue_occurrences order by occurrence_id")

    def tracking(self):
        names = self.sql(
            "select table_name from information_schema.tables where table_schema=%s and table_type='BASE TABLE' and table_name like 'dqm_%%' order by table_name",
            [self.schema],
        )
        return {
            row["table_name"]: self.sql(f"select * from @schema.{row['table_name']} order by 1,2")
            for row in names
        }


@pytest.fixture
def demo(tmp_path):
    schema = "dqm_it_" + uuid.uuid4().hex[:10]
    project = tmp_path / "project"
    shutil.copytree(
        ROOT / "integration_tests/demo_postgres",
        project,
        ignore=shutil.ignore_patterns("target", "logs", "dbt_packages"),
    )
    (project / "dbt_packages").mkdir()
    (project / "dbt_packages/dbt_dqm").symlink_to(ROOT, target_is_directory=True)
    (project / "packages.yml").write_text(yaml.safe_dump({"packages": [{"local": str(ROOT)}]}))
    # get_dsn_parameters() never returns the password, so a password-authenticated server (as in
    # CI) would receive an empty one. Merge it back from the DSN the caller supplied.
    with psycopg2.connect(DSN) as connection:
        p = {**connection.get_dsn_parameters(), **parse_dsn(DSN)}
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    output = {
        "type": "postgres",
        "host": p["host"],
        "port": int(p["port"]),
        "user": p["user"],
        "password": p.get("password", ""),
        "dbname": p["dbname"],
        "schema": schema,
        "threads": 2,
    }
    (profiles / "profiles.yml").write_text(
        yaml.safe_dump({"dbt_dqm_demo_postgres": {"target": "dev", "outputs": {"dev": output}}})
    )
    # Fault injection is confined to this temporary consumer project.
    config = yaml.safe_load((project / "dbt_project.yml").read_text())
    config["models"] = {
        "dbt_dqm": {
            "dqm_reconcile": {
                "+pre-hook": "{{ acceptance_pause() }}",
                "+post-hook": "{{ acceptance_fault() }}",
                "+persist_docs": {"relation": True, "columns": True},
                "+grants": {"select": [p["user"]]},
            }
        }
    }
    (project / "dbt_project.yml").write_text(yaml.safe_dump(config, sort_keys=False))
    (project / "macros/acceptance_hooks.sql").write_text("""
{% macro acceptance_pause() %}{% if execute and var('pause',false) and not dbt_dqm.empty_mode() %}select pg_sleep(4);{% endif %}{% endmacro %}
{% macro acceptance_fault() %}{% if execute and var('fault',false) and not dbt_dqm.empty_mode() %}do $$ begin raise exception 'injected apply failure'; end $$;{% endif %}{% if execute and var('hold',false) and not dbt_dqm.empty_mode() %}select pg_sleep(6);{% endif %}{% endmacro %}
{% macro postgres__migrations() %}
{% set registry=dbt_dqm.default__migrations() %}
{% if var('future',false) %}{% do registry.append({'id':'0002_acceptance','apply':'acceptance_add','backfill':'acceptance_backfill','verify':'acceptance_verify'}) %}{% endif %}
{{ return(registry) }}{% endmacro %}
{% macro default__acceptance_add() %}alter table {{ dbt_dqm.dqm_relation('dqm_issue_occurrences') }} add column if not exists future_field text;{% endmacro %}
{% macro default__acceptance_backfill() %}update {{ dbt_dqm.dqm_relation('dqm_issue_occurrences') }} set future_field='preserved' where future_field is null;{% endmacro %}
{% macro default__acceptance_verify() %}{{ dbt_dqm.assert_sql('not exists(select 1 from ' ~ dbt_dqm.dqm_relation('dqm_issue_occurrences') ~ ' where future_field is null)',"'backfill incomplete'") }}{% endmacro %}
""")
    # dbt runs package on-run-end hooks before the root project's, ordered by package name. This
    # package sorts before dbt_dqm, so it can change a stored-failure table after dbt computed
    # result.failures but before dbt-dqm captures it, as a concurrent invocation would.
    tamper = project / "dbt_packages/aaa_acceptance_tamper"
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
insert into "{{ target.schema }}_dbt_test__audit"."demo_customer_email_invalid" (customer_id, email, reason)
values ('tamper-row', 'tamper@example.com', 'tampered');
{% endif %}{% endmacro %}
""")
    instance = Demo(project, profiles, schema)
    try:
        instance.dbt("seed")
        yield instance
    finally:
        instance.sql("drop schema if exists @schema cascade")
        instance.sql(f'drop schema if exists "{schema}_dbt_test__audit" cascade')


def test_lifecycle_annotations_history_and_health(demo):
    demo.capture()
    demo.dbt("build", "--select", "package:dbt_dqm")
    initial = demo.occurrences()
    assert len(initial) == 7
    email = [row for row in initial if row["test_name"] == "demo_customer_email_invalid"]
    assert {row["poc_responsible"] for row in email} == {"customer_success", "customer_data_owner"}
    assert all("dqm_owner" not in row["record_values_json"].lower() for row in email)
    assert all(row["test_priority"] == "high" for row in email)
    assert demo.sql("select count(*) n from @schema.dqm_reconciliation_receipts")[0]["n"] == 3
    health = demo.sql(
        "select * from @schema.dqm_test_health where test_name='demo_customer_email_invalid'"
    )[0]
    assert health["reviewed_precision"] is None and health["assessed_count"] == 0
    assert health["review_population_count"] == 2 and health["collection_coverage"] == 1
    assert demo.sql("select * from @schema.dqm_area_health")
    demo.sql(
        "update @schema.dqm_issue_occurrences set workflow_status='RESOLVED',test_status='RESOLVED',notes='reviewed',annotation_version=4,review_verdict='TRUE_POSITIVE'"
    )
    demo.capture("passed")
    demo.dbt("run", "--select", "dqm_reconcile")
    closed = demo.occurrences()
    assert all(
        row["record_status"] == "Archived"
        and row["workflow_status_at_close"] == "RESOLVED"
        and row["annotation_version"] == 4
        for row in closed
    )
    # Public views remain present after reconciliation alone.
    assert len(demo.sql("select * from @schema.dqm_all_issues")) == 7
    demo.capture("recurrence")
    demo.dbt("run", "--select", "dqm_reconcile")
    recurrence = demo.sql("select * from @schema.dqm_current_issues")
    assert len(recurrence) == 3
    assert all(
        row["is_recurrence"]
        and row["is_potential_regression"]
        and row["previous_notes"] == "reviewed"
        for row in recurrence
    )
    assert all(row["notes"] is None and row["review_verdict"] == "UNREVIEWED" for row in recurrence)
    before = demo.occurrences()
    events = demo.sql("select * from @schema.dqm_issue_events order by event_id")
    demo.dbt("run", "--select", "dqm_reconcile")
    demo.dbt("run", "--select", "dqm_reconcile")
    assert demo.occurrences() == before  # includes timestamps and versions
    assert demo.sql("select * from @schema.dqm_issue_events order by event_id") == events
    assert {row["event_type"] for row in events} == {"APPEARED", "DISAPPEARED", "REAPPEARED"}
    demo.capture("recurrence", demo_priority=None)
    demo.dbt("run", "--select", "dqm_reconcile")
    assert all(
        row["test_priority"] is None
        for row in demo.occurrences()
        if row["record_status"] == "Active"
    )
    assert all(
        row["test_priority"] == "high"
        for row in demo.occurrences()
        if row["record_status"] == "Archived" and row["test_name"] == "demo_customer_email_invalid"
    )


def test_batched_replay_rollback_and_empty(demo):
    for scenario in ("initial", "passed", "recurrence"):
        demo.capture(scenario)
    demo.dbt("run", "--select", "dqm_reconcile", variables={"fault": True}, success=False)
    # First installation and apply are in the failed model transaction.
    assert not demo.sql(
        "select 1 from information_schema.tables where table_schema=%s and table_name='dqm_reconciliation_receipts'",
        [demo.schema],
    ) or not demo.sql("select * from @schema.dqm_reconciliation_receipts")
    demo.dbt("build", "--select", "package:dbt_dqm", variables={"demo_scenario": "recurrence"})
    assert len(demo.occurrences()) == 10
    before = demo.occurrences()
    demo.dbt("run", "--select", "dqm_reconcile", variables={"demo_scenario": "recurrence"})
    assert demo.occurrences() == before
    baseline = demo.tracking()
    for command in ("run", "build"):
        demo.dbt(
            command,
            "--empty",
            "--select",
            "package:dbt_dqm",
            variables={"demo_scenario": "recurrence"},
        )
        assert demo.tracking() == baseline
        # --empty disables tracking writes only; the public views keep serving real data.
        assert demo.sql("select count(*) n from @schema.dqm_all_issues")[0]["n"] == len(before)
        assert demo.sql("select count(*) n from @schema.dqm_issue_timeline")[0]["n"] == len(before)
        assert demo.sql("select count(*) n from @schema.dqm_test_health")[0]["n"] > 0


def test_reconciliation_serializes_and_reviewer_edit_survives(demo):
    demo.capture()
    demo.dbt("build", "--select", "package:dbt_dqm")
    row = demo.occurrences()[0]
    with ThreadPoolExecutor(3) as pool:
        first = pool.submit(demo.dbt, "run", "--select", "dqm_reconcile", variables={"pause": True})
        locked = False
        for _ in range(100):
            # A separate session must not acquire the transaction's advisory lock.
            with psycopg2.connect(DSN) as conn, conn.cursor() as cursor:
                cursor.execute(
                    "select pg_try_advisory_xact_lock(hashtext(%s))", ["dbt_dqm:" + demo.schema]
                )
                locked = not cursor.fetchone()[0]
            if locked:
                break
            time.sleep(0.1)
        assert locked
        reviewer = pool.submit(
            demo.sql,
            "update @schema.dqm_issue_occurrences set notes='concurrent edit',annotation_version=annotation_version+1 where occurrence_id=%s",
            [row["occurrence_id"]],
        )
        # Separate targets/log dirs avoid dbt artifact file races between processes.
        second = pool.submit(
            demo.dbt,
            "run",
            "--select",
            "dqm_reconcile",
            "--target-path",
            "target-second",
            "--log-path",
            str(demo.project / "logs-second"),
        )
        first.result()
        reviewer.result()
        second.result()
    updated = next(r for r in demo.occurrences() if r["occurrence_id"] == row["occurrence_id"])
    assert (
        updated["notes"] == "concurrent edit"
        and updated["annotation_version"] == row["annotation_version"] + 1
    )


def test_late_skip_cleanup_and_future_migration(demo):
    demo.capture()
    demo.dbt("build", "--select", "package:dbt_dqm")
    ids = demo.sql(
        "select test_unique_id from @schema.dqm_reconciliation_state order by test_unique_id limit 2"
    )
    for row in ids:
        demo.sql(
            "insert into @schema.dqm_test_executions(invocation_id,captured_at,test_unique_id,test_status,collection_status) values ('late-shared',current_timestamp-interval '1 day',%s,'pass','not_applicable')",
            [row["test_unique_id"]],
        )
    before = demo.occurrences()
    demo.dbt("run", "--select", "dqm_reconcile", success=False)
    assert demo.occurrences() == before
    items = [
        {"test_unique_id": row["test_unique_id"], "invocation_id": "late-shared"} for row in ids
    ]
    demo.dbt(
        "run-operation",
        "dqm_skip_late_evidence",
        "--args",
        json.dumps({"items": items, "reason": "Reviewed delayed capture"}),
    )
    skipped = demo.sql("select * from @schema.dqm_issue_events where event_type='EVIDENCE_SKIPPED'")
    assert len(skipped) == 2 and len({r["event_id"] for r in skipped}) == 2
    demo.dbt("run", "--select", "dqm_reconcile")
    assert demo.occurrences() == before
    demo.sql(
        "update @schema.dqm_test_executions set captured_at=current_timestamp-interval '40 day' where invocation_id in (select invocation_id from @schema.dqm_reconciliation_receipts)"
    )
    demo.sql(
        "insert into @schema.dqm_test_executions(invocation_id,captured_at,test_unique_id,test_status,collection_status) values ('pending-old',current_timestamp-interval '40 day','pending-test','pass','pending')"
    )
    event_count = len(demo.sql("select * from @schema.dqm_issue_events"))
    demo.dbt("run-operation", "cleanup_dqm_logs", variables={"dbt_dqm_retention_days": 1})
    assert demo.sql(
        "select invocation_id from @schema.dqm_test_executions where invocation_id='pending-old'"
    )
    assert len(demo.sql("select * from @schema.dqm_issue_events")) == event_count
    demo.dbt("run", "--select", "dqm_reconcile")
    assert demo.occurrences() == before
    demo.dbt(
        "run", "--select", "dqm_reconcile", variables={"future": True, "fault": True}, success=False
    )
    demo.dbt("run", "--select", "dqm_reconcile", variables={"future": True})
    after = demo.occurrences()
    assert [{k: v for k, v in row.items() if k != "future_field"} for row in after] == before
    assert all(row["future_field"] == "preserved" for row in after)
    demo.dbt("run", "--select", "dqm_reconcile", variables={"future": True})
    assert demo.occurrences() == after


def test_uninitialized_empty_and_legacy_guard(demo):
    result = demo.dbt("run", "--empty", "--select", "package:dbt_dqm", success=False)
    assert "initialized" in result.stdout
    demo.sql("create table @schema.dqm_issue_observations(dummy text)")
    result = demo.dbt("run", "--select", "dqm_reconcile", success=False)
    assert "fresh DQM schema" in result.stdout
    assert not demo.sql(
        "select 1 from information_schema.tables where table_schema=%s and table_name='dqm_install'",
        [demo.schema],
    )


def test_owner_conflicts_payload_events_and_structural_closures(demo):
    demo.capture(demo_owner_conflict=True)
    demo.dbt("build", "--select", "package:dbt_dqm")
    emails = demo.sql(
        "select * from @schema.dqm_issue_occurrences where test_name='demo_customer_email_invalid'"
    )
    assert all(row["poc_responsible"] == "customer_data_owner" for row in emails)
    conflicts = demo.sql(
        "select owner_conflict_identity_count from @schema.dqm_test_executions where test_name='demo_customer_email_invalid'"
    )
    assert conflicts[0]["owner_conflict_identity_count"] == 1
    demo.sql(
        "update @schema.dqm_issue_occurrences set poc_responsible='reviewer' where test_name='demo_customer_email_invalid'"
    )
    demo.capture("recurrence")  # No intervening pass: continuing failure with different payload.
    demo.dbt("run", "--select", "dqm_reconcile")
    current = demo.sql(
        "select * from @schema.dqm_current_issues where test_name='demo_customer_email_invalid'"
    )
    assert current[0]["poc_responsible"] == "reviewer" and not current[0]["is_recurrence"]
    changed = demo.sql("select * from @schema.dqm_issue_events where event_type='VALUES_CHANGED'")
    assert changed and all(
        row["previous_record_values_json"] != row["record_values_json"] for row in changed
    )
    demo.capture("recurrence", demo_grain=["customer_id", "email"])
    demo.dbt("run", "--select", "dqm_reconcile", variables={"demo_grain": ["customer_id", "email"]})
    assert demo.sql(
        "select 1 from @schema.dqm_issue_occurrences where close_reason='IDENTITY_CHANGED'"
    )
    assert not demo.sql(
        "select 1 from @schema.dqm_current_issues where test_name='demo_customer_email_invalid' and is_recurrence"
    )
    (demo.project / "tests/demo_customer_email_invalid.sql").unlink()
    demo.dbt("run", "--select", "dqm_reconcile")
    assert demo.sql("select 1 from @schema.dqm_issue_occurrences where close_reason='TEST_REMOVED'")


def test_app_verdict_audit_missed_submission_and_shared_dependencies(demo):
    from dbt_dqm_app.config import load_config
    from dbt_dqm_app.store import Patch
    from dbt_dqm_app.warehouse import apply_patches, insert_missed_issue

    (demo.project / "models/sources.yml").write_text(
        "version: 2\nsources:\n  - name: external\n    tables:\n      - name: customers\n"
    )
    (demo.project / "tests/demo_shared.sql").write_text("""
{{ config(tags=['dqm'],store_failures=true,meta={'dbt_dqm':{'granularity':['customer_id']}}) }}
-- depends_on: {{ source('external','customers') }}
select customer_id from {{ ref('demo_records') }} where false
""")
    demo.capture()
    demo.dbt("build", "--select", "package:dbt_dqm")
    config = load_config(demo.project, demo.profiles, "dev")
    row = demo.occurrences()[0]
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
    audited = demo.sql(
        "select * from @schema.dqm_annotation_changes where field_name='review_verdict'"
    )
    assert (
        len(audited) == 1
        and audited[0]["resulting_annotation_version"] == row["annotation_version"] + 1
    )
    missed = {
        "missed_issue_id": "retry-id",
        "area_label": "Unmapped campaign",
        "description": "Confirmed escape",
        "root_cause": "no_test",
        "discovered_at": datetime.now(UTC).isoformat(),
    }
    insert_missed_issue(config, missed)
    insert_missed_issue(config, missed)
    assert demo.sql("select count(*) n from @schema.dqm_missed_issues")[0]["n"] == 1
    source = demo.sql(
        "select * from @schema.dqm_area_health where source_unique_id like 'source.%'"
    )
    assert source[0]["tracked_test_count"] == 1
    unmapped = demo.sql("select * from @schema.dqm_area_health where source_unique_id is null")
    assert unmapped[0]["known_missed_issue_count"] == 1
    assert demo.sql("select sum(assessed_count) n from @schema.dqm_test_health")[0]["n"] == 1


def test_first_package_build_and_interrupted_setup_resume(demo):
    # App-written models create their tables once, even when no capture has run yet.
    demo.dbt("run", "--select", "package:dbt_dqm")
    assert (
        demo.sql("select * from @schema.dqm_schema_migrations")[0]["migration_id"] == "0001_initial"
    )
    assert demo.sql("select count(*) n from @schema.dqm_annotation_changes")[0]["n"] == 0
    assert demo.sql("select count(*) n from @schema.dqm_missed_issues")[0]["n"] == 0
    demo.sql("delete from @schema.dqm_schema_migrations")
    demo.sql("update @schema.dqm_reconciliation_control set schema_version=null")
    demo.dbt("run", "--select", "dqm_reconcile")
    assert (
        demo.sql("select * from @schema.dqm_schema_migrations")[0]["migration_id"] == "0001_initial"
    )


def test_health_includes_outstanding_evidence_older_than_window(demo):
    demo.dbt("run", "--select", "package:dbt_dqm")
    test_id = "test.dbt_dqm_demo_postgres.demo_customer_email_invalid"
    for invocation, age, status in (
        ("old-conclusive", 40, "not_applicable"),
        ("old-pending", 50, "pending"),
    ):
        demo.sql(
            "insert into @schema.dqm_test_executions(invocation_id,captured_at,test_unique_id,test_status,collection_status) values (%s,current_timestamp-(%s * interval '1 day'),%s,'pass',%s)",
            [invocation, age, test_id, status],
        )
    health = demo.sql("select * from @schema.dqm_test_health where test_unique_id=%s", [test_id])[0]
    assert health["recorded_execution_count"] == 0
    assert health["collection_coverage"] is None
    assert health["processing_lag_count"] == 1 and health["pending_collection_count"] == 1
    assert "lag," in health["attention_flags"]
    demo.dbt("run", "--select", "dqm_reconcile")
    health = demo.sql("select * from @schema.dqm_test_health where test_unique_id=%s", [test_id])[0]
    assert health["processing_lag_count"] == 0 and health["pending_collection_count"] == 1


def _latest_execution(demo, test_name):
    return demo.sql(
        "select * from @schema.dqm_test_executions where test_name=%s order by captured_at desc limit 1",
        [test_name],
    )[0]


def test_capture_rejects_failure_tables_changed_before_capture(demo):
    customer = "demo_customer_email_invalid"

    def customer_rows():
        return [row for row in demo.occurrences() if row["test_name"] == customer]

    demo.capture()
    demo.dbt("build", "--select", "package:dbt_dqm")
    baseline = customer_rows()
    assert baseline and all(row["record_status"] == "Active" for row in baseline)

    # A failing test whose table gained a row after dbt counted it: no evidence is recorded.
    demo.capture(tamper=True)
    tampered = _latest_execution(demo, customer)
    assert tampered["collection_status"] == "collection_error"
    assert "Stored failures changed before capture" in tampered["collection_message"]
    assert not demo.sql(
        "select 1 from @schema.dqm_issue_observations where invocation_id=%s and test_unique_id=%s",
        [tampered["invocation_id"], tampered["test_unique_id"]],
    )
    assert _latest_execution(demo, "demo_order_amount_invalid")["collection_status"] == "success"
    demo.dbt("run", "--select", "dqm_reconcile")
    assert customer_rows() == baseline

    # A passing test whose table isn't empty at capture time is never treated as a pass.
    demo.capture("passed", tamper=True)
    tampered_pass = _latest_execution(demo, customer)
    assert tampered_pass["collection_status"] == "collection_error"
    assert "dbt reported 0 rows, the table had 1" in tampered_pass["collection_message"]
    assert (
        _latest_execution(demo, "demo_order_amount_invalid")["collection_status"]
        == "not_applicable"
    )
    demo.dbt("run", "--select", "dqm_reconcile")
    assert all(row["record_status"] == "Active" for row in customer_rows())
    assert all(
        row["record_status"] == "Archived" and row["close_reason"] == "Passed"
        for row in demo.occurrences()
        if row["test_name"] != customer
    )

    # An untampered pass is still a pass.
    demo.capture("passed")
    assert _latest_execution(demo, customer)["collection_status"] == "not_applicable"
    demo.dbt("run", "--select", "dqm_reconcile")
    assert all(row["record_status"] == "Archived" for row in customer_rows())


def _wait_for_exclusive_occurrence_lock(demo):
    for _ in range(150):
        held = demo.sql(
            "select 1 from pg_locks l join pg_class c on c.oid=l.relation "
            "join pg_namespace n on n.oid=c.relnamespace "
            "where n.nspname=%s and c.relname='dqm_issue_occurrences' "
            "and l.mode='ExclusiveLock' and l.granted",
            [demo.schema],
        )
        if held:
            return
        time.sleep(0.1)
    raise AssertionError("reconciliation never took the occurrence table lock")


def test_app_write_fails_fast_while_reconciliation_holds_the_table(demo):
    from dataclasses import replace

    from dbt_dqm_app.config import load_config
    from dbt_dqm_app.errors import WarehouseBusy, classify
    from dbt_dqm_app.store import Patch
    from dbt_dqm_app.warehouse import apply_patches

    demo.capture()
    demo.dbt("build", "--select", "package:dbt_dqm")
    config = replace(load_config(demo.project, demo.profiles, "dev"), lock_timeout_seconds=1)
    row = demo.occurrences()[0]
    patch = Patch(
        row["occurrence_id"],
        "notes",
        None,
        "edited after reconciliation",
        1,
        datetime.now(UTC).isoformat(),
        row["annotation_version"],
    )
    with ThreadPoolExecutor(1) as pool:
        # The post-hook sleeps inside the reconciliation transaction, holding its table lock.
        running = pool.submit(
            demo.dbt, "run", "--select", "dqm_reconcile", variables={"hold": True}
        )
        _wait_for_exclusive_occurrence_lock(demo)
        started = time.monotonic()
        with pytest.raises(Exception) as caught:
            apply_patches(config, [patch])
        assert time.monotonic() - started < 4
        assert isinstance(classify(caught.value), WarehouseBusy)
        running.result()
    unchanged = next(r for r in demo.occurrences() if r["occurrence_id"] == row["occurrence_id"])
    assert unchanged["notes"] == row["notes"]
    assert unchanged["annotation_version"] == row["annotation_version"]
    assert not demo.sql("select 1 from @schema.dqm_annotation_changes")
    # Once the reconciliation commits, the same patch applies normally.
    apply_patches(config, [patch])
    applied = next(r for r in demo.occurrences() if r["occurrence_id"] == row["occurrence_id"])
    assert applied["notes"] == "edited after reconciliation"
