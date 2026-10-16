"""Warehouse acceptance tests. Set DBT_DQM_TEST_DSN to a disposable PostgreSQL database.

Every test creates its own schema and removes that schema afterward. No consumer data is read.
"""

from __future__ import annotations

import json
import os
import re
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
        command += ["--vars", json.dumps({"dqm_enabled": True, **(variables or {})})]
        result = subprocess.run(command, capture_output=True, text=True, timeout=90, check=False)
        if args[0] in ("test", "build", "run"):
            from explicit_capture import collect

            result = collect(command, result, self.project / "target")
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
        "dbt_dqm_demo_postgres": {
            "demo_records": {"+pre-hook": "{{ acceptance_records_fault() }}"},
        },
        "dbt_dqm": {
            "dqm_reconcile": {
                "+pre-hook": "{{ acceptance_pause() }}",
                "+post-hook": "{{ acceptance_fault() }}",
                "+persist_docs": {"relation": True, "columns": True},
                "+grants": {"select": [p["user"]]},
            }
        },
    }
    (project / "dbt_project.yml").write_text(yaml.safe_dump(config, sort_keys=False))
    (project / "macros/acceptance_hooks.sql").write_text("""
{% macro acceptance_pause() %}{% if execute and var('pause',false) and not dbt_dqm.empty_mode() %}select pg_sleep(4);{% endif %}{% endmacro %}
{% macro acceptance_fault() %}{% if execute and var('fault',false) and not dbt_dqm.empty_mode() %}do $$ begin raise exception 'injected apply failure'; end $$;{% endif %}{% if execute and var('hold',false) and not dbt_dqm.empty_mode() %}select pg_sleep(6);{% endif %}{% endmacro %}
{% macro postgres__migrations() %}
{% set registry=dbt_dqm.default__migrations() %}
{% if var('future',false) %}{% do registry.append({'id':'9999_acceptance','apply':'acceptance_add','backfill':'acceptance_backfill','verify':'acceptance_verify'}) %}{% endif %}
{{ return(registry) }}{% endmacro %}
{% macro default__acceptance_add() %}alter table {{ dbt_dqm.dqm_relation('dqm_issue_occurrences') }} add column if not exists future_field text;{% endmacro %}
{% macro default__acceptance_backfill() %}update {{ dbt_dqm.dqm_relation('dqm_issue_occurrences') }} set future_field='preserved' where future_field is null;{% endmacro %}
{% macro postgres__maintenance_raw_pruning(days) %}{{ dbt_dqm.default__maintenance_raw_pruning(days) }}{% if var('fail_step','')=='raw_pruning' %} raise exception '{{ var("fail_marker","injected") }}';{% endif %}{% endmacro %}
{% macro postgres__maintenance_event_retention(events_days) %}{{ dbt_dqm.default__maintenance_event_retention(events_days) }}{% if var('fail_step','')=='event_retention' %} raise exception '{{ var("fail_marker","injected") }}';{% endif %}{% endmacro %}
{% macro postgres__maintenance_log_pruning() %}{{ dbt_dqm.default__maintenance_log_pruning() }}{% if var('fail_step','')=='log_pruning' %} raise exception '{{ var("fail_marker","injected") }}';{% endif %}{% endmacro %}
{% macro postgres__maintenance_log_insert_sql(step_name, ok_expression, diagnostic_expression, marker, kind) %}{% if var('fail_log',false) %} raise exception 'maintenance log unavailable';{% else %}{{ dbt_dqm.default__maintenance_log_insert_sql(step_name, ok_expression, diagnostic_expression, marker, kind) }}{% endif %}{% endmacro %}
{% macro acceptance_records_fault() %}{% if execute and var('fail_records',false) %}do $$ begin raise exception 'injected model failure'; end $$;{% endif %}{% endmacro %}
{% macro postgres__browse_keyset_index_verify() %}{{ dbt_dqm.default__browse_keyset_index_verify() }}{% if var('interrupt_0007',false) %} raise exception 'injected 0007 interruption';{% endif %}{% endmacro %}
{% macro postgres__maintenance_attribution_verify() %}{{ dbt_dqm.default__maintenance_attribution_verify() }}{% if var('interrupt_0006',false) %} raise exception 'injected 0006 interruption';{% endif %}{% endmacro %}
{% macro postgres__maintenance_log_verify() %}{{ dbt_dqm.default__maintenance_log_verify() }}{% if var('interrupt_0005',false) %} raise exception 'injected 0005 interruption';{% endif %}{% endmacro %}
{% macro postgres__event_payload_mode_backfill() %}{{ dbt_dqm.default__event_payload_mode_backfill() }}{% if var('interrupt_0002',false) %} raise exception 'injected 0002 interruption';{% endif %}{% endmacro %}
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
    # dbt 1.12 requires every installed package to be declared, so the tamper package is listed
    # next to dbt_dqm and the demo's lock file (which names only dbt_dqm) is dropped.
    (project / "package-lock.yml").unlink(missing_ok=True)
    (project / "packages.yml").write_text(
        yaml.safe_dump({"packages": [{"local": str(ROOT)}, {"local": str(tamper)}]})
    )
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
    assert {row["dqm_owner"] for row in email} == {"customer_success", "customer_data_owner"}
    assert all("dqm_owner" in row["record_values_json"].lower() for row in email)
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
        for _ in range(300):
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
    assert all(row["dqm_owner"] == "customer_data_owner" for row in emails)
    conflicts = demo.sql(
        "select owner_conflict_identity_count from @schema.dqm_test_executions where test_name='demo_customer_email_invalid'"
    )
    assert conflicts[0]["owner_conflict_identity_count"] == 1
    demo.sql(
        "update @schema.dqm_issue_occurrences set dqm_owner='reviewer' where test_name='demo_customer_email_invalid'"
    )
    demo.capture("recurrence")  # No intervening pass: continuing failure with different payload.
    demo.dbt("run", "--select", "dqm_reconcile")
    current = demo.sql(
        "select * from @schema.dqm_current_issues where test_name='demo_customer_email_invalid'"
    )
    assert current[0]["dqm_owner"] == "reviewer" and not current[0]["is_recurrence"]
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
     installation_id=row.get("_installation_id") or demo.sql("select installation_id from @schema.dqm_install")[0]["installation_id"])
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


def test_warehouse_browsing_matches_the_snapshot_and_sees_generation_changes(demo):
    from browse_checks import check_browsing, check_page_edit

    from dbt_dqm_app.browse import Filters
    from dbt_dqm_app.config import load_config
    from dbt_dqm_app.warehouse import browse_page, browse_population, browse_summary

    demo.capture()
    demo.dbt("build", "--select", "package:dbt_dqm")
    config = load_config(demo.project, demo.profiles, "dev")
    generation = check_browsing(config)
    check_page_edit(config, demo.project.parent / "workspace.sqlite")
    demo.capture("passed")
    demo.dbt("run", "--select", "dqm_reconcile")
    population = browse_population(config)
    archived = browse_page(config, population, Filters(lifecycle=("Archived",)), None, 25)
    assert archived.generation != generation  # the app restarts paging instead of mixing pages
    assert len(archived.rows) == 7
    summary = browse_summary(config, population, Filters())
    assert summary["active_count"] == summary["matching"] == 0


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
    from dbt_dqm_app.relations import resolve_config
    from dbt_dqm_app.store import Patch
    from dbt_dqm_app.warehouse import apply_patches

    demo.capture()
    demo.dbt("build", "--select", "package:dbt_dqm")
    # Match app startup: resolve naming before timing warehouse lock refusal.
    config = resolve_config(
        replace(load_config(demo.project, demo.profiles, "dev"), lock_timeout_seconds=1)
    )
    row = demo.occurrences()[0]
    patch = Patch(
        row["occurrence_id"],
        "notes",
        None,
        "edited after reconciliation",
        1,
        datetime.now(UTC).isoformat(),
        row["annotation_version"],
     installation_id=row.get("_installation_id") or demo.sql("select installation_id from @schema.dqm_install")[0]["installation_id"])
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


def test_capture_completing_after_a_later_run_is_late_evidence(demo):
    """A real capture whose collection commits after a later capture was reconciled.

    Holding the first capture at 'pending' stands in for its transaction committing late; the
    reconciliation in between must exclude it, and once it becomes visible it is older than the
    processed high-water and must be rejected before any lifecycle write.
    """
    customer = "demo_customer_email_invalid"
    demo.capture()
    demo.dbt("run", "--select", "dqm_reconcile")
    demo.capture("passed")
    slow = _latest_execution(demo, customer)
    demo.sql(
        "update @schema.dqm_test_executions set collection_status='pending' where invocation_id=%s",
        [slow["invocation_id"]],
    )
    demo.capture("recurrence")
    demo.dbt("run", "--select", "dqm_reconcile")
    assert not demo.sql(
        "select 1 from @schema.dqm_reconciliation_receipts where invocation_id=%s",
        [slow["invocation_id"]],
    )
    before = demo.occurrences()
    receipts = demo.sql("select * from @schema.dqm_reconciliation_receipts order by 1,2")

    demo.sql(
        "update @schema.dqm_test_executions set collection_status=%s "
        "where invocation_id=%s and test_unique_id=%s",
        [slow["collection_status"], slow["invocation_id"], slow["test_unique_id"]],
    )
    late = demo.dbt("run", "--select", "dqm_reconcile", success=False)
    assert "Late DQM evidence" in late.stdout
    assert slow["invocation_id"] in late.stdout
    assert demo.occurrences() == before
    assert demo.sql("select * from @schema.dqm_reconciliation_receipts order by 1,2") == receipts

    items = [{"test_unique_id": slow["test_unique_id"], "invocation_id": slow["invocation_id"]}]
    demo.dbt(
        "run-operation",
        "dqm_skip_late_evidence",
        "--args",
        json.dumps({"items": items, "reason": "Capture committed after a later run"}),
    )
    demo.dbt("run", "--select", "dqm_reconcile")
    assert demo.occurrences() == before
    skipped = demo.sql(
        "select * from @schema.dqm_issue_events where event_type='EVIDENCE_SKIPPED' "
        "and invocation_id=%s",
        [slow["invocation_id"]],
    )
    assert len(skipped) == len(items)


def test_restricted_runner_and_reviewer_use_only_documented_grants(demo):
    """A non-superuser runner builds everything; a reviewer with only the documented grants can
    perform every app operation and nothing else."""
    from browse_checks import check_browsing

    from dbt_dqm_app.config import load_config
    from dbt_dqm_app.store import Patch
    from dbt_dqm_app.warehouse import apply_patches, fetch_health, fetch_issues, insert_missed_issue

    suffix = demo.schema.removeprefix("dqm_it_")
    runner, reviewer = f"dqm_runner_{suffix}", f"dqm_reviewer_{suffix}"
    password = f"restricted-{suffix}"
    schema = f"{demo.schema}_restricted"
    dbname = parse_dsn(DSN)["dbname"]
    for role in (runner, reviewer):
        demo.sql(f'create role "{role}" login password %s', [password])
    demo.sql(f'grant create on database "{dbname}" to "{runner}"')

    def profiles_for(user, directory):
        directory.mkdir()
        profile = yaml.safe_load((demo.profiles / "profiles.yml").read_text())
        output = profile["dbt_dqm_demo_postgres"]["outputs"]["dev"]
        output.update({"user": user, "password": password, "schema": schema})
        (directory / "profiles.yml").write_text(yaml.safe_dump(profile))
        return directory

    config_path = demo.project / "dbt_project.yml"
    project = yaml.safe_load(config_path.read_text())
    project["models"]["dbt_dqm"]["+grants"] = {"select": [reviewer]}
    project.setdefault("vars", {})["dbt_dqm_table_grants"] = {
        "dqm_install": {"select": [reviewer]},
        "dqm_reconciliation_control": {"select": [reviewer]},
        "dqm_issue_occurrences": {"select": [reviewer], "update": [reviewer]},
        "dqm_annotation_changes": {"select": [reviewer], "insert": [reviewer]},
        "dqm_missed_issues": {"select": [reviewer], "insert": [reviewer]},
    }
    config_path.write_text(yaml.safe_dump(project, sort_keys=False))
    as_runner = Demo(demo.project, profiles_for(runner, demo.project.parent / "runner"), schema)
    try:
        as_runner.dbt("seed")
        as_runner.capture()
        as_runner.dbt("build", "--select", "package:dbt_dqm")
        as_runner.capture("passed")
        as_runner.dbt("run", "--select", "dqm_reconcile")
        as_runner.sql(f'grant usage on schema "{schema}" to "{reviewer}"')

        reviewer_profiles = profiles_for(reviewer, demo.project.parent / "reviewer")
        config = load_config(demo.project, reviewer_profiles, "dev")
        issues = fetch_issues(config)
        assert len(issues) == len(as_runner.occurrences())
        assert fetch_health(config)["tests"]
        check_browsing(config, "Archived")
        row = issues[0]
        patch = Patch(
            row["occurrence_id"],
            "notes",
            row.get("notes"),
            "reviewed with restricted grants",
            1,
            datetime.now(UTC).isoformat(),
            row["annotation_version"],
         installation_id=row.get("_installation_id") or demo.sql("select installation_id from @schema.dqm_install")[0]["installation_id"])
        apply_patches(config, [patch])
        insert_missed_issue(
            config,
            {
                "missed_issue_id": f"restricted-{suffix}",
                "area_label": "Restricted reviewer check",
                "description": "Filed by the restricted reviewer",
                "root_cause": "no_test",
                "discovered_at": datetime.now(UTC).isoformat(),
            },
        )

        reviewer_dsn = " ".join(
            f"{key}={value}"
            for key, value in {**parse_dsn(DSN), "user": reviewer, "password": password}.items()
        )
        for statement in (
            f'delete from "{schema}".dqm_issue_occurrences',
            f'update "{schema}".dqm_reconciliation_control set generation = generation + 1',
            (
                f'insert into "{schema}".dqm_reconciliation_receipts '
                "(test_unique_id, invocation_id) values ('x', 'y')"
            ),
            f'select * from "{schema}".dqm_test_executions',
        ):
            with (
                psycopg2.connect(reviewer_dsn) as conn,
                conn.cursor() as cursor,
                pytest.raises(psycopg2.errors.InsufficientPrivilege),
            ):
                cursor.execute(statement)
    finally:
        as_runner.sql(f'drop schema if exists "{schema}" cascade')
        as_runner.sql(f'drop schema if exists "{schema}_dbt_test__audit" cascade')
        for role in (runner, reviewer):
            as_runner.sql(f'drop owned by "{role}" cascade')
            as_runner.sql(f'drop role "{role}"')


@pytest.mark.parametrize("mode", ["full", "changed_columns", "none"])
def test_event_payload_modes(demo, mode):
    variables = {"dbt_dqm_event_payloads": mode}
    demo.capture()
    demo.dbt("build", "--select", "package:dbt_dqm", variables=variables)
    demo.capture("recurrence")  # Continuing failures with changed payloads.
    demo.dbt("run", "--select", "dqm_reconcile", variables=variables)
    changed = demo.sql("select * from @schema.dqm_issue_events where event_type='VALUES_CHANGED'")
    appeared = demo.sql("select * from @schema.dqm_issue_events where event_type='APPEARED'")
    assert changed and appeared
    assert {row["payload_mode"] for row in changed + appeared} == {mode}
    for row in changed:
        if mode == "full":
            before = json.loads(row["previous_record_values_json"])
            after = json.loads(row["record_values_json"])
            expected = sorted(
                k
                for k in before.keys() | after.keys()
                if before.get(k, object()) != after.get(k, object())
            )
            assert row["changed_columns"] == ",".join(expected)
        else:
            assert row["previous_record_values_json"] is None and row["record_values_json"] is None
        if mode == "none":
            assert row["changed_columns"] is None and row["payload_digest"] is None
        else:
            assert row["changed_columns"]
            assert row["previous_payload_digest"] and row["payload_digest"]
            assert row["previous_payload_digest"] != row["payload_digest"]


def test_event_payload_migration_upgrades_an_existing_02_schema(demo):
    demo.capture()
    demo.dbt("build", "--select", "package:dbt_dqm")
    demo.capture("passed")
    demo.dbt("run", "--select", "dqm_reconcile")
    demo.sql(
        "update @schema.dqm_issue_occurrences set notes='kept through 0002', "
        "review_verdict='TRUE_POSITIVE', annotation_version=annotation_version+1"
    )
    # Turn the schema back into a populated 0001-only 0.2 install.
    columns = ("payload_mode", "changed_columns", "previous_payload_digest", "payload_digest")
    for column in columns:
        demo.sql(f"alter table @schema.dqm_issue_events drop column {column}")
    demo.sql("delete from @schema.dqm_schema_migrations where migration_id <> '0001_initial'")
    demo.sql("update @schema.dqm_reconciliation_control set schema_version='0001_initial'")
    before = demo.occurrences()
    history = demo.sql("select * from @schema.dqm_issue_events order by event_id")
    assert history

    # Interrupted: the migration runs inside the reconcile transaction, so nothing persists.
    demo.dbt("run", "--select", "dqm_reconcile", variables={"interrupt_0002": True}, success=False)
    assert demo.occurrences() == before
    assert not demo.sql(
        "select 1 from information_schema.columns where table_schema=%s "
        "and table_name='dqm_issue_events' and column_name='payload_mode'",
        [demo.schema],
    )
    assert (
        demo.sql("select schema_version from @schema.dqm_reconciliation_control")[0][
            "schema_version"
        ]
        == "0001_initial"
    )

    demo.dbt("run", "--select", "dqm_reconcile")
    assert demo.occurrences() == before
    migrated = demo.sql("select * from @schema.dqm_issue_events order by event_id")
    assert [{k: v for k, v in row.items() if k not in columns} for row in migrated] == history
    assert {row["payload_mode"] for row in migrated} == {"full"}
    assert {
        row["migration_id"] for row in demo.sql("select * from @schema.dqm_schema_migrations")
    } == {
        "0001_initial",
        "0002_event_payload_mode",
        "0003_app_change_staging",
        "0004_app_staging_safety",
        "0005_maintenance_log",
        "0006_maintenance_attribution",
        "0007_browse_keyset_index",
    }
    control = demo.sql("select * from @schema.dqm_reconciliation_control")[0]
    assert (
        control["schema_version"] == "0007_browse_keyset_index"
        and control["setup_status"] == "ready"
    )


RETENTION = {"dbt_dqm_retention_days": 30, "dbt_dqm_event_retention_days": 365}


def _maintenance(demo, invocation_filter=""):
    return demo.sql(
        "select * from @schema.dqm_maintenance_log "
        + invocation_filter
        + " order by logged_at, step"
    )


def _health(demo):
    return {
        row["step"]: row
        for row in demo.sql("select * from @schema.dqm_maintenance_health order by step")
    }


def test_maintenance_failures_are_logged_safely_and_never_fail_the_invocation(demo):
    demo.capture(**RETENTION)
    demo.dbt("build", "--select", "package:dbt_dqm", variables=RETENTION)
    assert {row["step"] for row in _maintenance(demo)} >= {"raw_pruning", "event_retention"}

    # A failing step: the invocation still succeeds, the other steps run, and nothing from the
    # warehouse error text (which here carries a secret marker) reaches the log.
    marker = "dqm-secret-" + uuid.uuid4().hex
    demo.capture(**RETENTION, fail_step="event_retention", fail_marker=marker)
    rows = demo.sql("select * from @schema.dqm_maintenance_log")
    failed = [row for row in rows if row["outcome"] == "failed"]
    assert [row["step"] for row in failed] == ["event_retention"]
    assert failed[0]["diagnostic_id"] == "P0001"  # SQLSTATE raise_exception, not message text
    assert failed[0]["description"] == "event retention failed"
    assert all(marker not in json.dumps(row, default=str) for row in rows)
    # The health view already exists from the build; re-running a package model would itself
    # trigger maintenance (and recover the step).
    assert _health(demo)["event_retention"]["state"] == "failing"
    assert _health(demo)["raw_pruning"]["state"] == "ok"

    # The next run retries and recovers.
    demo.capture(**RETENTION)
    assert _health(demo)["event_retention"]["state"] == "ok"

    # Raw pruning fails after its deletes: everything in that step is rolled back.
    demo.sql(
        "update @schema.dqm_test_executions set captured_at=captured_at - interval '400 day' "
        "where invocation_id in (select invocation_id from @schema.dqm_reconciliation_receipts)"
    )
    before = {
        name: demo.sql(f"select count(*) n from @schema.{name}")[0]["n"]
        for name in ("dqm_issue_observations", "dqm_reconciliation_receipts", "dqm_test_executions")
    }
    control = demo.sql(
        "select generation, raw_pruned_before from @schema.dqm_reconciliation_control"
    )
    demo.capture(**RETENTION, fail_step="raw_pruning")
    after = {name: demo.sql(f"select count(*) n from @schema.{name}")[0]["n"] for name in before}
    assert after["dqm_reconciliation_receipts"] == before["dqm_reconciliation_receipts"]
    assert after["dqm_issue_observations"] >= before["dqm_issue_observations"]
    assert (
        demo.sql("select raw_pruned_before from @schema.dqm_reconciliation_control")[0][
            "raw_pruned_before"
        ]
        == control[0]["raw_pruned_before"]
    )
    assert _health(demo)["raw_pruning"]["state"] == "failing"

    # A log-pruning failure can't remove the outcome rows just written.
    demo.capture(**RETENTION, fail_step="log_pruning")
    latest = demo.sql(
        "select step, outcome from @schema.dqm_maintenance_log where invocation_id="
        "(select invocation_id from @schema.dqm_maintenance_log order by logged_at desc limit 1)"
    )
    assert {row["step"] for row in latest} >= {"raw_pruning", "event_retention", "log_pruning"}
    assert {row["outcome"] for row in latest if row["step"] == "log_pruning"} == {"failed"}

    # If outcomes can't be logged, the run still succeeds and health is unknown, never ok.
    demo.capture(**RETENTION, fail_log=True)
    assert {row["state"] for row in _health(demo).values()} == {"unknown"}


def test_maintenance_scope_and_failure_boundary(demo):
    demo.capture(**RETENTION)
    demo.dbt("build", "--select", "package:dbt_dqm", variables=RETENTION)
    logged = len(_maintenance(demo))
    # A run that touches no dbt-dqm models or tracked tests performs no maintenance.
    demo.dbt("run", "--select", "demo_records", variables=RETENTION)
    assert len(_maintenance(demo)) == logged
    # Reconciliation failures are not maintenance: they still fail the invocation.
    demo.capture(**RETENTION)
    demo.dbt(
        "run", "--select", "dqm_reconcile", variables={**RETENTION, "fault": True}, success=False
    )


def test_migration_0005_upgrades_a_populated_schema_after_interruption(demo):
    demo.capture()
    demo.dbt("build", "--select", "package:dbt_dqm")
    demo.sql("drop table @schema.dqm_maintenance_log cascade")  # a 0004 schema has neither
    demo.sql(
        "delete from @schema.dqm_schema_migrations "
        "where migration_id in "
        "('0005_maintenance_log', '0006_maintenance_attribution', '0007_browse_keyset_index')"
    )
    demo.sql(
        "update @schema.dqm_reconciliation_control set schema_version='0004_app_staging_safety'"
    )
    before = demo.occurrences()
    # Migrations are not optional maintenance: an interrupted one fails visibly and rolls back.
    demo.dbt("run", "--select", "dqm_reconcile", variables={"interrupt_0005": True}, success=False)
    assert not demo.sql(
        "select 1 from information_schema.tables where table_schema=%s and table_name='dqm_maintenance_log'",
        [demo.schema],
    )
    demo.dbt("run", "--select", "dqm_reconcile")
    assert demo.occurrences() == before
    assert demo.sql("select count(*) n from @schema.dqm_maintenance_log")[0]["n"] == 0
    control = demo.sql("select * from @schema.dqm_reconciliation_control")[0]
    assert (
        control["schema_version"] == "0007_browse_keyset_index"
        and control["setup_status"] == "ready"
    )


def _run_row(demo, run_id, at):
    demo.sql(
        "insert into @schema.dqm_reconciliation_runs "
        "(run_id, generation_at_freeze, stage_relation, status, started_at, completed_at) "
        "values (%s, 0, 'pg_temp.dqm_stage', 'completed', %s, %s)",
        [run_id, at, at],
    )


def _outcome(demo, invocation, outcome, marker, kind="automatic", logged=None, step="raw_pruning"):
    demo.sql(
        "insert into @schema.dqm_maintenance_log (logged_at, invocation_id, step, outcome, "
        "description, diagnostic_id, trigger_marker_at, trigger_kind) "
        "values (%s, %s, %s, %s, 'fixture', null, %s, %s)",
        [logged or marker or "2026-01-01 12:00", invocation, step, outcome, marker, kind],
    )


def test_maintenance_health_contract(demo):
    """Health states from controlled triggers and outcomes (one step, raw_pruning)."""
    demo.capture(**RETENTION)
    demo.dbt("build", "--select", "package:dbt_dqm", variables=RETENTION)

    def scenario(build):
        for table in (
            "dqm_maintenance_log",
            "dqm_reconciliation_inputs",
            "dqm_reconciliation_runs",
            "dqm_test_executions",
        ):
            demo.sql(f"delete from @schema.{table}")
        build()
        return _health(demo)["raw_pruning"]["state"]

    t = {n: f"2026-01-01 0{n}:00" for n in range(1, 6)}

    def no_outcome():
        _run_row(demo, "a", t[1])

    def success():
        _run_row(demo, "a", t[1])
        _outcome(demo, "a", "succeeded", t[1])

    def earlier_success_newer_missing():
        _run_row(demo, "a", t[1])
        _run_row(demo, "b", t[2])
        _outcome(demo, "a", "succeeded", t[1], logged=t[3])

    def later_success_supersedes_older_missing():
        # Accepted approximation: the older-marker trigger may still be running.
        _run_row(demo, "a", t[1])
        _run_row(demo, "b", t[2])
        _outcome(demo, "b", "succeeded", t[2])

    def tie_with_missing():
        _run_row(demo, "a", t[1])
        _run_row(demo, "b", t[1])
        _outcome(demo, "a", "succeeded", t[1])

    def tie_with_failure():
        tie_with_missing()
        _outcome(demo, "b", "failed", t[1])

    def conflicting_outcomes():
        _run_row(demo, "a", t[1])
        _outcome(demo, "a", "succeeded", t[1])
        _outcome(demo, "a", "failed", t[1])

    def failure_without_success():
        _run_row(demo, "a", t[1])
        _outcome(demo, "a", "failed", t[1])

    def failure_then_missing():
        failure_without_success()
        _run_row(demo, "b", t[2])

    def out_of_order_logging():
        # The later-marker success is logged first; the older failure logged later can't move
        # the baseline back.
        _run_row(demo, "a", t[1])
        _run_row(demo, "b", t[2])
        _outcome(demo, "b", "succeeded", t[2], logged=t[3])
        _outcome(demo, "a", "failed", t[1], logged=t[4])

    def legacy_only():
        _outcome(demo, "legacy", "succeeded", None, kind=None, logged=t[1])

    def legacy_superseded():
        legacy_only()
        _run_row(demo, "a", t[2])
        _outcome(demo, "a", "succeeded", t[2], logged=t[3])

    def unattributed_after_success():
        success()
        _outcome(demo, "b", "succeeded", None, logged=t[3])

    def manual_success_then_new_trigger():
        _run_row(demo, "a", t[1])
        _outcome(demo, "manual", "succeeded", None, kind="manual", logged=t[2])
        _run_row(demo, "b", t[3])

    def manual_success_after_triggers():
        _run_row(demo, "a", t[1])
        _outcome(demo, "manual", "succeeded", None, kind="manual", logged=t[2])

    def pruned_trigger_history():
        _outcome(demo, "a", "succeeded", t[1])  # its run row is gone; the log keeps the marker

    expected = {
        no_outcome: "unknown",
        success: "ok",
        earlier_success_newer_missing: "unknown",
        later_success_supersedes_older_missing: "ok",
        tie_with_missing: "unknown",
        tie_with_failure: "failing",
        conflicting_outcomes: "unknown",
        failure_without_success: "failing",
        failure_then_missing: "unknown",
        out_of_order_logging: "ok",
        legacy_only: "unknown",
        legacy_superseded: "ok",
        unattributed_after_success: "unknown",
        manual_success_then_new_trigger: "unknown",
        manual_success_after_triggers: "ok",
        pruned_trigger_history: "ok",
    }
    actual = {case.__name__: scenario(case) for case in expected}
    assert actual == {case.__name__: state for case, state in expected.items()}


def test_maintenance_attribution_and_reconcile_only_recovery(demo):
    demo.capture(**RETENTION)
    demo.dbt("build", "--select", "package:dbt_dqm", variables=RETENTION)
    demo.capture(**RETENTION)
    invocation = demo.sql(
        "select invocation_id, max(captured_at) as marker from @schema.dqm_test_executions "
        "group by invocation_id order by marker desc limit 1"
    )[0]
    rows = _maintenance(demo, f"where invocation_id='{invocation['invocation_id']}'")
    # Every outcome of the invocation carries the same marker, read before any step ran.
    assert {row["trigger_kind"] for row in rows} == {"automatic"}
    assert {row["trigger_marker_at"] for row in rows} == {invocation["marker"]}
    assert {row["state"] for row in _health(demo).values()} == {"ok"}

    # A reconciliation-only run is a trigger. If it can't log, health is unknown, not ok...
    demo.dbt("run", "--select", "dqm_reconcile", variables={**RETENTION, "fail_log": True})
    assert {row["state"] for row in _health(demo).values()} == {"unknown"}
    # ...and the next successful run restores it.
    demo.dbt("run", "--select", "dqm_reconcile", variables=RETENTION)
    assert {row["state"] for row in _health(demo).values()} == {"ok"}
    run = demo.sql(
        "select run_id, started_at from @schema.dqm_reconciliation_runs "
        "order by started_at desc limit 1"
    )[0]
    latest = _maintenance(demo, f"where invocation_id='{run['run_id']}'")
    assert latest and {row["trigger_marker_at"] for row in latest} == {run["started_at"]}


def test_explicit_cleanup_requires_a_recorded_success_for_every_step(demo):
    demo.capture(**RETENTION)
    # Postgres without retention settings schedules nothing: a valid no-op.
    before = len(_maintenance(demo))
    demo.dbt("run-operation", "cleanup_dqm_logs")
    assert len(_maintenance(demo)) == before

    result = demo.dbt(
        "run-operation",
        "cleanup_dqm_logs",
        variables={**RETENTION, "fail_log": True},
        success=False,
    )
    assert "raw_pruning (outcome not recorded)" in result.stdout
    result = demo.dbt(
        "run-operation",
        "cleanup_dqm_logs",
        variables={**RETENTION, "fail_step": "event_retention"},
        success=False,
    )
    assert "event_retention (failed, diagnostic P0001)" in result.stdout
    demo.dbt("run-operation", "cleanup_dqm_logs", variables=RETENTION)
    manual = _maintenance(demo, "where trigger_kind='manual'")
    assert manual and {row["trigger_marker_at"] for row in manual} == {None}


def test_maintenance_triggers(demo):
    demo.capture(**RETENTION)
    demo.dbt("build", "--select", "package:dbt_dqm", variables=RETENTION)

    def new_rows(*args, success=True, **variables):
        before = {row["invocation_id"] for row in _maintenance(demo)}
        demo.dbt(*args, variables={**RETENTION, **variables}, success=success)
        return [row for row in _maintenance(demo) if row["invocation_id"] not in before]

    steps = {"raw_pruning", "event_retention", "run_pruning", "log_pruning"}
    # Package views without reconciliation, unrelated models and --empty: no maintenance.
    assert new_rows("run", "--select", "dqm_current_issues") == []
    assert new_rows("run", "--select", "demo_records") == []
    assert new_rows("build", "--select", "package:dbt_dqm", "--empty") == []
    # A failed reconciliation is not a trigger (and still fails the invocation).
    assert new_rows("run", "--select", "dqm_reconcile", success=False, fault=True) == []
    # Mixed build: models, tracked tests and reconciliation run maintenance once.
    mixed = new_rows("build", "--select", "demo_records", "tag:dqm", "package:dbt_dqm")
    assert sorted(row["step"] for row in mixed) == sorted(steps)
    # Skipped tracked tests are still captured, so they are a trigger with a recorded marker.
    skipped = new_rows(
        "build", "--select", "demo_records", "tag:dqm", success=False, fail_records=True
    )
    assert sorted(row["step"] for row in skipped) == sorted(steps)
    invocation = skipped[0]["invocation_id"]
    executions = demo.sql(
        "select test_status, captured_at from @schema.dqm_test_executions where invocation_id=%s",
        [invocation],
    )
    assert executions and {row["test_status"] for row in executions} == {"skipped"}
    assert {row["trigger_marker_at"] for row in skipped} == {
        max(row["captured_at"] for row in executions)
    }


def test_migration_0006_adds_attribution_to_a_populated_log(demo):
    demo.capture(**RETENTION)
    demo.dbt("build", "--select", "package:dbt_dqm", variables=RETENTION)
    # A 0005 schema: the health view (which reads the new columns) is rebuilt by the package.
    demo.sql("drop view @schema.dqm_maintenance_health")
    demo.sql(
        "alter table @schema.dqm_maintenance_log "
        "drop column trigger_marker_at, drop column trigger_kind"
    )
    demo.sql(
        "delete from @schema.dqm_schema_migrations "
        "where migration_id in ('0006_maintenance_attribution', '0007_browse_keyset_index')"
    )
    demo.sql("update @schema.dqm_reconciliation_control set schema_version='0005_maintenance_log'")
    legacy = _maintenance(demo)
    before = demo.occurrences()
    # Migrations are not optional maintenance: an interrupted one fails visibly and rolls back.
    demo.dbt("run", "--select", "dqm_reconcile", variables={"interrupt_0006": True}, success=False)
    columns = demo.sql(
        "select column_name, data_type from information_schema.columns "
        "where table_schema=%s and table_name='dqm_maintenance_log'",
        [demo.schema],
    )
    assert {"trigger_marker_at", "trigger_kind"}.isdisjoint(row["column_name"] for row in columns)
    demo.dbt("build", "--select", "package:dbt_dqm")
    columns = {
        row["column_name"]: row["data_type"]
        for row in demo.sql(
            "select column_name, data_type from information_schema.columns "
            "where table_schema=%s and table_name='dqm_maintenance_log'",
            [demo.schema],
        )
    }
    assert columns["trigger_marker_at"] == "timestamp without time zone"
    assert columns["trigger_kind"] == "text"
    assert demo.occurrences() == before
    preserved = demo.sql(
        "select * from @schema.dqm_maintenance_log where invocation_id = any(%s)",
        [[row["invocation_id"] for row in legacy]],
    )
    assert len(preserved) == len(legacy)
    assert {row["trigger_kind"] for row in preserved} == {None}
    control = demo.sql("select * from @schema.dqm_reconciliation_control")[0]
    assert control["schema_version"] == "0007_browse_keyset_index"


def test_migration_0007_adds_the_browse_index_after_interruption(demo):
    from dbt_dqm_app.browse import Filters, page_query

    demo.capture()
    demo.dbt("build", "--select", "package:dbt_dqm")
    index = (
        "select indexdef from pg_indexes where schemaname=%s "
        "and indexname='dqm_occurrence_browse_active'"
    )
    assert demo.sql(index, [demo.schema])  # fresh installs get it too
    # A 0006 schema.
    demo.sql("drop index @schema.dqm_occurrence_browse_active")
    demo.sql(
        "delete from @schema.dqm_schema_migrations where migration_id='0007_browse_keyset_index'"
    )
    demo.sql(
        "update @schema.dqm_reconciliation_control set schema_version='0006_maintenance_attribution'"
    )
    before = demo.occurrences()
    demo.dbt("run", "--select", "dqm_reconcile", variables={"interrupt_0007": True}, success=False)
    assert not demo.sql(index, [demo.schema])  # rolled back with the failed migration
    demo.dbt("run", "--select", "dqm_reconcile")
    assert demo.sql(index, [demo.schema])
    assert demo.occurrences() == before
    control = demo.sql("select * from @schema.dqm_reconciliation_control")[0]
    assert control["schema_version"] == "0007_browse_keyset_index"
    assert control["setup_status"] == "ready"
    # The app's page query matches the index expressions, so the planner can use it.
    query = page_query("postgres", f'"{demo.schema}".dqm_all_issues', "record_status = 'Active'",
                       Filters(), None, 25)
    with psycopg2.connect(DSN) as conn, conn.cursor() as cursor:
        cursor.execute("set enable_seqscan = off; set enable_bitmapscan = off")
        cursor.execute("explain " + query.sql, query.params)
        plan = "\n".join(row[0] for row in cursor.fetchall())
    assert "dqm_occurrence_browse_active" in plan, plan


RACE_PROBE = """{{{{ config(materialized='{materialization}',
    pre_hook=["select pg_advisory_xact_lock(hashtext('dqm-race-probe'))"]) }}}}
select 1 as probe
"""
# Test-only: pause dbt's backup-relation drop on an advisory-lock barrier held by the test.
RACE_BARRIER = """
{% macro postgres__drop_relation(relation) -%}
  {% if var('race_barrier', false) and relation.identifier.endswith('__dbt_backup') %}
    {% call statement('race_barrier', auto_begin=False) -%}
      select pg_advisory_lock(hashtext('dqm-race-barrier')); select pg_advisory_unlock(hashtext('dqm-race-barrier'));
    {%- endcall %}
  {% endif %}
  {{ return(dbt.default__drop_relation(relation)) }}
{%- endmacro %}
"""


def _race_project(demo):
    for name, materialization in (
        ("race_probe_default", "view"),
        ("race_probe_entry", "dqm_entry_view"),
    ):
        (demo.project / f"models/{name}.sql").write_text(
            RACE_PROBE.format(materialization=materialization)
        )
    (demo.project / "macros/race_barrier.sql").write_text(RACE_BARRIER)


def _dbt_logged(demo, model, log_dir, variables=None):
    demo.dbt(
        "run",
        "--select",
        model,
        "--log-path",
        str(log_dir),
        "--target-path",
        str(log_dir / "target"),
        variables=variables,
    )
    return (log_dir / "dbt.log").read_text()


def _probe_relations(demo):
    return {
        row["relname"]
        for row in demo.sql(
            "select c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace "
            "where n.nspname=%s and c.relname like 'race_probe%%'",
            [demo.schema],
        )
    }


def test_default_view_materialization_drops_another_runs_backup_before_its_lock(demo):
    """Demonstrates the unsafe pre-lock interleaving behind #17 with dbt's default view
    materialization: session B drops session A's committed backup relation before B takes
    the lock, using its own relation cache. The exact `drop external` path is not reproduced."""
    _race_project(demo)
    demo.dbt("run", "--select", "race_probe_default")  # The view exists before the race.
    with psycopg2.connect(DSN) as barrier, barrier.cursor() as cursor:
        barrier.autocommit = True
        cursor.execute("select pg_advisory_lock(hashtext('dqm-race-barrier'))")
        with ThreadPoolExecutor(1) as pool:
            first = pool.submit(
                _dbt_logged,
                demo,
                "race_probe_default",
                demo.project / "logs-a",
                {"race_barrier": True},
            )
            # A commits, then waits at the barrier before dropping its own backup relation.
            for _ in range(300):
                if "race_probe_default__dbt_backup" in _probe_relations(demo):
                    break
                time.sleep(0.1)
            assert "race_probe_default__dbt_backup" in _probe_relations(demo)
            second = _dbt_logged(demo, "race_probe_default", demo.project / "logs-b")
            cursor.execute("select pg_advisory_unlock(hashtext('dqm-race-barrier'))")
            first.result()
    drop = re.search(
        r'drop view if exists "[^"]+"\."[^"]+"\."race_probe_default__dbt_backup"', second
    )
    lock_at = second.find("pg_advisory_xact_lock(hashtext('dqm-race-probe'))")
    assert drop is not None and 0 <= drop.start() < lock_at, (
        "session B dropped A's backup relation before taking the lock"
    )


def test_entry_view_performs_no_ddl_before_the_lock(demo):
    _race_project(demo)
    demo.dbt("run", "--select", "race_probe_entry")
    with psycopg2.connect(DSN) as holder, holder.cursor() as cursor:
        holder.autocommit = True
        cursor.execute("select pg_advisory_lock(hashtext('dqm-race-probe'))")
        with ThreadPoolExecutor(1) as pool:
            waiting = pool.submit(_dbt_logged, demo, "race_probe_entry", demo.project / "logs-b")
            time.sleep(5)
            assert not waiting.done(), "the run must wait for the lock"
            assert _probe_relations(demo) == {"race_probe_entry"}
            cursor.execute("select pg_advisory_unlock(hashtext('dqm-race-probe'))")
            log = waiting.result()
    lock_at = log.find("pg_advisory_xact_lock(hashtext('dqm-race-probe'))")
    assert lock_at >= 0
    before_lock = log[:lock_at].lower()
    assert "drop view" not in before_lock and "drop table" not in before_lock
    assert "rename to" not in before_lock and "create or replace view" not in before_lock
    assert "create or replace view" in log[lock_at:].lower()
    assert _probe_relations(demo) == {"race_probe_entry"}


def test_entry_view_overlapping_reconciliations_repeat(demo):
    demo.capture()
    demo.dbt("build", "--select", "package:dbt_dqm")
    for attempt in range(20):
        with ThreadPoolExecutor(2) as pool:
            runs = [
                pool.submit(
                    demo.dbt,
                    "run",
                    "--select",
                    "dqm_reconcile",
                    "--target-path",
                    f"target-{attempt}-{index}",
                    "--log-path",
                    str(demo.project / f"logs-{attempt}-{index}"),
                )
                for index in range(2)
            ]
            for run in runs:
                run.result()
    assert not demo.sql(
        "select 1 from pg_class c join pg_namespace n on n.oid=c.relnamespace "
        "where n.nspname=%s and c.relname like 'dqm_reconcile__dbt%%'",
        [demo.schema],
    )


def test_entry_view_appends_migrated_columns_and_keeps_public_views(demo):
    demo.capture()
    demo.dbt("build", "--select", "package:dbt_dqm")
    issues = demo.sql("select count(*) n from @schema.dqm_all_issues")[0]["n"]
    demo.dbt("run", "--select", "dqm_reconcile", variables={"future": True})
    columns = [
        row["column_name"]
        for row in demo.sql(
            "select column_name from information_schema.columns where table_schema=%s "
            "and table_name='dqm_reconcile' order by ordinal_position",
            [demo.schema],
        )
    ]
    assert columns[-1] == "future_field"
    assert demo.sql("select count(*) n from @schema.dqm_all_issues")[0]["n"] == issues
    assert demo.sql("select count(*) n from @schema.dqm_reconcile")[0]["n"] >= issues


def test_entry_view_refuses_to_replace_a_table(demo):
    demo.dbt("run", "--select", "dqm_reconcile")
    demo.sql("drop view @schema.dqm_reconcile")
    demo.sql("create table @schema.dqm_reconcile (kept text)")
    demo.sql("insert into @schema.dqm_reconcile values ('untouched')")
    failed = demo.dbt("run", "--select", "dqm_reconcile", success=False)
    assert "expected" in failed.stdout and "to be a view" in failed.stdout
    assert demo.sql("select kept from @schema.dqm_reconcile") == [{"kept": "untouched"}]


def test_package_initialization_and_app_with_custom_aliases(demo):
    from alias_checks import NAMING_MACROS, check_alias_app

    project = demo.project
    # Preserve existing canonical tables when a consumer enables a new alias macro.
    demo.dbt("run", "--select", "package:dbt_dqm", "--threads", "4")
    demo.sql("insert into @schema.dqm_missed_issues (missed_issue_id,description) "
             "values ('kept-before-alias','existing canonical data')")
    demo.dbt("run", "--select", "demo_records")
    (project / "macros/consumer_naming.sql").write_text(NAMING_MACROS, encoding="utf-8")
    refused = dedicated(demo, code=2)
    assert "Legacy shared" in refused.stderr
    dedicated(demo, "--full-refresh")
    # First install and idempotent reruns must work at the normal four-thread setting.
    for _ in range(3):
        demo.dbt("run", "--select", "package:dbt_dqm", "--threads", "4")
    demo.capture()
    demo.dbt("build", "--select", "package:dbt_dqm")
    check_alias_app(project, demo.profiles)
    assert demo.sql("select description from @schema.dqm_missed_issues "
                    "where missed_issue_id='kept-before-alias'")[0]["description"] == "existing canonical data"


def dedicated(demo, *options, variables=None, code=0, confirm=True):
    """Exercise the actual isolated product command, without the protocol-harness phases."""
    command = [str(Path(sys.executable).with_name("dbt-dqm")), "run",
               "--project-dir", str(demo.project), "--profiles-dir", str(demo.profiles), *options]
    if "--full-refresh" in options and confirm:
        command.append("--yes")
    if variables:
        command += ["--vars", json.dumps(variables)]
    result = subprocess.run(command, text=True, capture_output=True, timeout=120, check=False)
    assert result.returncode == code, result.stdout + result.stderr
    return result


def test_dedicated_native_limits_complete_pass_and_stale_edits(demo):
    from dbt_dqm_app.config import load_config
    from dbt_dqm_app.errors import AppError
    from dbt_dqm_app.store import Workspace
    from dbt_dqm_app.warehouse import apply_patches, fetch_issues, validate_dbt

    demo.dbt("run", "--select", "demo_records")
    # An ordinary command with DQM disabled runs neither package models nor consumers' DQM tests.
    ordinary = subprocess.run([str(Path(sys.executable).with_name("dbt")), "build", "--select", "package:dbt_dqm",
                               "--project-dir", str(demo.project), "--profiles-dir", str(demo.profiles)],
                              capture_output=True, text=True, timeout=90, check=False)
    assert ordinary.returncode == 0, ordinary.stdout + ordinary.stderr
    assert not demo.sql("select 1 from information_schema.tables where table_schema=%s and table_name='dqm_install'", [demo.schema])
    dedicated(demo, "--threads", "4")
    assert len(demo.occurrences()) == 7
    original = json.loads((demo.project / "target/dqm_run_results.json").read_text())
    invocation = original["metadata"]["invocation_id"]
    executions = demo.sql("select test_unique_id,invocation_id from @schema.dqm_test_executions")
    assert {row["invocation_id"] for row in executions} == {invocation}
    assert {row["test_unique_id"] for row in executions} == {
        row["unique_id"] for row in original["results"] if row["unique_id"].startswith("test.")}
    empty = dedicated(demo, "--select", "no_such_test")
    assert "0 tests" in empty.stdout
    assert len(demo.occurrences()) == 7
    assert all(row["record_status"] == "Active" for row in demo.occurrences())
    config = load_config(demo.project, demo.profiles, "dev")
    workspace = Workspace(demo.project.parent / "review.sqlite")
    workspace.replace_snapshot(fetch_issues(config))
    workspace.set_change(workspace.rows()[0]["occurrence_id"], "notes", "stale after reset")
    patches = workspace.pending()
    epoch = patches[0].installation_id
    limited = dedicated(demo, variables={"dbt_dqm_default_test_limit": 2})
    assert "DQM phase seconds" not in limited.stdout
    assert "DQM complete:" in limited.stdout
    assert "Compiled node" not in limited.stdout and "Running with dbt" not in limited.stdout
    rows = demo.sql("select * from @schema.dqm_test_executions where failure_limit=2")
    assert len(rows) == 3 and {row["collection_status"] for row in rows} == {"limit_reached"}
    assert sorted(row["captured_issue_count"] for row in rows) == [1, 2, 2]
    assert all(row["record_status"] == "Active" for row in demo.occurrences())
    sample = demo.sql("select test_unique_id,unique_id,record_values_json from @schema.dqm_issue_observations "
                      "where invocation_id=%s order by test_unique_id,unique_id", [rows[0]["invocation_id"]])
    dedicated(demo, variables={"dbt_dqm_default_test_limit": 2})
    latest = demo.sql("select invocation_id from @schema.dqm_test_executions order by captured_at desc limit 1")[0]["invocation_id"]
    assert demo.sql("select test_unique_id,unique_id,record_values_json from @schema.dqm_issue_observations "
                    "where invocation_id=%s order by test_unique_id,unique_id", [latest]) == sample
    health = demo.sql("select * from @schema.dqm_test_health")
    assert all(row["limit_reached_count"] == 2 for row in health)
    demo.dbt("run", "--select", "demo_records", variables={"demo_scenario": "passed"})
    dedicated(demo)
    assert all(row["record_status"] == "Archived" for row in demo.occurrences())
    demo.sql("create table @schema.consumer_keep as select 42 as value")
    before_reset = demo.occurrences() if hasattr(demo, "occurrences") else demo.snapshot()
    refused_reset = dedicated(demo, "--full-refresh", confirm=False, code=2)
    assert "requires --yes" in refused_reset.stderr
    assert (demo.occurrences() if hasattr(demo, "occurrences") else demo.snapshot()) == before_reset
    dedicated(demo, "--full-refresh")
    assert demo.sql("select * from @schema.consumer_keep") == [{"value": 42}]
    assert demo.sql("select installation_id from @schema.dqm_install")[0]["installation_id"] != epoch
    with pytest.raises(AppError, match="Stale DQM installation"):
        apply_patches(config, patches)
    # App parsing re-enables manifest relations without executing models or tests.
    before = demo.tracking()
    validate_dbt(config)
    assert demo.tracking() == before


def test_dedicated_preflight_failure_codes_and_default_limit(demo):
    demo.dbt("run", "--select", "demo_records")
    dedicated(demo)
    initial = demo.sql("select * from @schema.dqm_install")
    invalid = dedicated(demo, "--select", "demo_invalid_empty_grain", "--full-refresh",
                        variables={"demo_invalid_configurations": True}, code=2)
    assert "demo_invalid_empty_grain" in invalid.stderr and "tests/demo_invalid_empty_grain.sql" in invalid.stderr
    assert demo.sql("select * from @schema.dqm_install") == initial
    missing = dedicated(demo, "--select", "demo_invalid_missing_column", "--full-refresh",
                        variables={"demo_invalid_configurations": True}, code=2)
    assert "missing_identity_column" in missing.stdout + missing.stderr
    assert demo.sql("select * from @schema.dqm_install") == initial
    test = demo.project / "tests/demo_customer_email_invalid.sql"
    source = test.read_text().replace("limit=var('dbt_dqm_default_test_limit',100),", "")
    source = source.replace("severity='warn'", "severity='error'")
    test.write_text(source)
    failed = dedicated(demo, "--select", "demo_customer_email_invalid",
                       variables={"dbt_dqm_default_test_limit": 1}, code=1)
    assert "native limit missing; using 1" in failed.stdout
    execution = _latest_execution(demo, "demo_customer_email_invalid")
    assert execution["failure_limit"] == 1 and execution["collection_status"] == "limit_reached"
    assert demo.sql("select 1 from @schema.dqm_reconciliation_receipts where invocation_id=%s",
                    [execution["invocation_id"]])
    tampered = dedicated(demo, variables={"tamper": True}, code=2)
    assert "collection_error" in tampered.stderr and "Stored failures changed before capture" in tampered.stderr
    assert _latest_execution(demo, "demo_order_amount_invalid")["collection_status"] == "success"


def test_dedicated_defer_inherited_owner_lists_and_custom_artifacts(demo, monkeypatch):
    from dbt_dqm_app.config import load_config
    from dbt_dqm_app.warehouse import fetch_issues

    demo.dbt("run", "--select", "demo_records")
    shutil.copytree(demo.project / "target", demo.project / "prod state")
    original_schema = demo.schema
    dev_schema = original_schema + "_dev"
    profile = yaml.safe_load((demo.profiles / "profiles.yml").read_text())
    profile["dbt_dqm_demo_postgres"]["outputs"]["dev"]["schema"] = dev_schema
    (demo.profiles / "profiles.yml").write_text(yaml.safe_dump(profile))
    project = yaml.safe_load((demo.project / "dbt_project.yml").read_text())
    project["data_tests"] = {"dbt_dqm_demo_postgres": {"+meta": {"owner": [" alpha ", "", "beta"]}}}
    (demo.project / "dbt_project.yml").write_text(yaml.safe_dump(project))
    test = demo.project / "tests/demo_customer_email_invalid.sql"
    test.write_text(test.read_text().replace("'owner': 'customer_data_owner',", ""))
    flags = ["--select", "demo_customer_email_invalid", "--state", "prod state",
             "--target-path", "dev artifacts"]
    demo.schema = dev_schema
    try:
        result = dedicated(demo, *flags, "--defer", "--favor-state")
        assert "2 captured issues" in result.stdout
        assert "Compiled node" not in result.stdout and "Running with dbt" not in result.stdout
        assert not demo.sql("select 1 from information_schema.tables where table_schema=%s and table_name='demo_records'", [dev_schema])
        owners = {row["dqm_owner"] for row in demo.occurrences()}
        assert owners == {"customer_success", "alpha, beta"}
        installation = demo.sql("select installation_id from @schema.dqm_install")
        project["data_tests"]["dbt_dqm_demo_postgres"]["+meta"]["owner"] = ["alpha", 7]
        (demo.project / "dbt_project.yml").write_text(yaml.safe_dump(project))
        invalid = dedicated(demo, *flags, "--defer", "--full-refresh", code=2)
        assert "meta.owner" in invalid.stderr and "['alpha', 7]" in invalid.stderr
        assert "tests/demo_customer_email_invalid.sql" in invalid.stderr
        assert demo.sql("select installation_id from @schema.dqm_install") == installation
        project["data_tests"]["dbt_dqm_demo_postgres"]["+meta"]["owner"] = [" alpha ", "", "beta"]
        (demo.project / "dbt_project.yml").write_text(yaml.safe_dump(project))
        invocation = json.loads((demo.project / "dev artifacts/dqm_run_results.json").read_text())["metadata"]["invocation_id"]
        assert demo.sql("select test_meta from @schema.dqm_test_executions where invocation_id=%s", [invocation])[0]["test_meta"].find('" alpha "') >= 0
        config = load_config(demo.project, demo.profiles, "dev", target_path="dev artifacts")
        assert len(fetch_issues(config)) == 2
        demo.sql("update @schema.dqm_issue_occurrences set dqm_owner='reviewer',annotation_version=1 where dqm_owner='alpha, beta'")
        dedicated(demo, *flags, "--defer", "--favor-state")
        assert {row["dqm_owner"] for row in demo.occurrences()} == {"customer_success", "reviewer"}
        monkeypatch.setenv("DBT_DEFER", "true")
        refused = dedicated(demo, *flags, "--no-defer", "--full-refresh", code=2)
        assert "does not exist" in refused.stdout + refused.stderr
        assert len(demo.occurrences()) == 2
        demo.sql(f'create table @schema.demo_records as select * from "{original_schema}".demo_records where false')
        favored = dedicated(demo, *flags, "--favor-state", "--verbose")
        assert "DQM phase seconds" in favored.stdout and "Running with dbt" in favored.stdout
        assert "2 captured issues" in favored.stdout
        dedicated(demo, *flags, "--no-favor-state")
        assert all(row["record_status"] == "Archived" for row in demo.occurrences())
    finally:
        demo.sql("drop schema if exists @schema cascade")
        demo.sql(f'drop schema if exists "{dev_schema}_dbt_test__audit" cascade')
        demo.schema = original_schema


def test_dedicated_full_refresh_aliases_grants_and_external_dependency_failure(demo):
    from dbt_dqm_app.config import load_config
    from dbt_dqm_app.warehouse import fetch_issues, validate_dbt

    base = demo.schema
    demo.dbt("run", "--select", "demo_records")
    (demo.project / "macros/naming.sql").write_text("""
{% macro generate_alias_name(custom_alias_name=none,node=none) %}
{{ ('reviewer_' if node.package_name=='dbt_dqm' else '') ~ (custom_alias_name|trim if custom_alias_name else node.name) }}
{% endmacro %}
{% macro generate_schema_name(custom_schema_name,node) %}
{{ target.schema ~ '_review' if node.package_name=='dbt_dqm' else target.schema ~ ('_' ~ custom_schema_name|trim if custom_schema_name else '') }}
{% endmacro %}
""")
    demo.schema = base + "_review"
    try:
        dedicated(demo)
        assert demo.sql("select count(*) n from @schema.reviewer_dqm_issue_occurrences")[0]["n"] == 7
        demo.sql("grant select on @schema.reviewer_dqm_all_issues to public")
        demo.sql("grant select on @schema.reviewer_dqm_issue_observations to public")
        before = demo.sql("select * from @schema.reviewer_dqm_install")
        demo.sql("create view @schema.consumer_dependent as select occurrence_id from @schema.reviewer_dqm_all_issues")
        refused = dedicated(demo, "--full-refresh", code=2)
        assert "depend" in refused.stderr.lower()
        assert demo.sql("select * from @schema.reviewer_dqm_install") == before
        assert demo.sql("select count(*) n from @schema.consumer_dependent")[0]["n"] == 7
        demo.sql("drop view @schema.consumer_dependent")
        demo.sql("create table @schema.consumer_keep as select 'preserved'::text as value")
        dedicated(demo, "--full-refresh")
        assert demo.sql("select * from @schema.consumer_keep") == [{"value": "preserved"}]
        grants = demo.sql("select table_name from information_schema.table_privileges "
                          "where table_schema=%s and grantee='PUBLIC' and privilege_type='SELECT'", [demo.schema])
        assert {row["table_name"] for row in grants} >= {"reviewer_dqm_all_issues", "reviewer_dqm_issue_observations"}
        assert not demo.sql("select pending_view_grants from @schema.reviewer_dqm_install")[0]["pending_view_grants"]
        config = load_config(demo.project, demo.profiles, "dev")
        validate_dbt(config)
        assert len(fetch_issues(config)) == 7
        assert demo.sql(f'select count(*) n from "{base}".demo_records')[0]["n"] > 0
        assert demo.sql(f'select count(*) n from "{base}_dbt_test__audit".demo_customer_email_invalid')[0]["n"] == 3
    finally:
        demo.sql("drop schema if exists @schema cascade")
        demo.schema = base


def test_two_developer_namespaces_are_isolated(demo):
    from namespace_checks import check_namespace_isolation
    check_namespace_isolation(demo, dedicated)


def test_run_changes_sorted_results_and_collection_limits(demo):
    from reporting_checks import check_run_reporting

    check_run_reporting(demo, dedicated)
