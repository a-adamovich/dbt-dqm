"""Compile both transaction protocols without making any warehouse calls."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
import yaml
from dbt.adapters.bigquery.connections import BigQueryConnectionManager
from dbt.adapters.contracts.connection import ConnectionState
from dbt.cli.main import dbtRunner

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def protocol_project(tmp_path_factory):
    path = tmp_path_factory.mktemp("compile-protocol")
    project = path / "project"
    shutil.copytree(
        ROOT / "integration_tests/demo_bigquery",
        project,
        ignore=shutil.ignore_patterns("target", "logs", "dbt_packages"),
    )
    (project / "dbt_packages").mkdir()
    (project / "dbt_packages/dbt_dqm").symlink_to(ROOT, target_is_directory=True)
    profiles = path / "profiles"
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
                            "project": "compile-only",
                            "dataset": "dqm",
                            "threads": 1,
                        }
                    },
                }
            }
        )
    )
    return project, profiles


def render(protocol_project, monkeypatch, macro, arguments="", variables=None):
    project, profiles = protocol_project

    def closed(cls, connection):
        connection.state = ConnectionState.CLOSED
        return connection

    def reject(*args, **kwargs):
        raise AssertionError("Compile attempted warehouse access")

    monkeypatch.setattr(BigQueryConnectionManager, "close", classmethod(closed))
    monkeypatch.setattr(BigQueryConnectionManager, "open", classmethod(reject))
    result = dbtRunner().invoke(
        [
            "--no-populate-cache",
            "compile",
            "--no-introspect",
            "--inline",
            "{{ dbt_dqm." + macro + "(" + arguments + ") }}",
            "--project-dir",
            str(project),
            "--profiles-dir",
            str(profiles),
            "--vars", json.dumps({"dqm_enabled": True, **(variables or {})}),
        ]
    )
    assert result.success, result.exception
    return result.result[0].node.compiled_code


@pytest.mark.parametrize("value, expected", [
    ([" alpha ", "", "beta"], "alpha, beta"), (" team ", "team"),
    ([], "None"), (["", " "], "None"), (None, "None"),
])
def test_owner_normalization_uses_all_nonblank_strings(protocol_project, monkeypatch, value, expected):
    # JSON null is not a Jinja literal; pass the value through vars instead.
    sql = render(protocol_project, monkeypatch, "owner_text", "var('test_owner')",
                 variables={"test_owner": value})
    assert sql.strip() == expected


def test_maintenance_probe_is_protected_and_preserves_live_work(protocol_project, monkeypatch):
    sql = render(protocol_project, monkeypatch, "bigquery__maintenance_probe_script",
                 "dbt_dqm.maintenance_steps(30,90),30,90")
    assert "set dqm_trigger_marker" in sql
    assert "exception when error then" in sql
    assert "= null;" in sql and "setup_status='ready'" in sql
    assert "status!='started'" in sql
    assert "status='abandoned'" in sql
    assert "interval 24 hour" in sql
    assert "delete from" not in sql


def test_empty_raw_pruning_keeps_the_cutoff_and_generation(protocol_project, monkeypatch):
    sql = render(protocol_project, monkeypatch, "default__maintenance_raw_pruning", "30",
                 variables={"dbt_dqm_maintenance_shortcuts": True})
    assert sql.index("generation=generation+1") < sql.index("if exists(")
    assert "raw_pruned_before" in sql and "run.status='started'" in sql
    assert sql.index("if exists(") < sql.index("create or replace temp table dqm_prune")
    assert "end if;" in sql


def test_empty_outcome_has_normal_health_attribution(protocol_project, monkeypatch):
    sql = render(protocol_project, monkeypatch, "bigquery__maintenance_log_script",
                 "[{'step':'staging_expiry','ok':true,'job_id':none,'empty':true}],"
                 "\"timestamp '2026-10-10T00:00:00.000000Z'\",'automatic'")
    assert "no eligible work" in sql and "'succeeded'" in sql
    assert "trigger_marker_at,trigger_kind" in sql and "'automatic'" in sql


def test_custom_dispatch_disables_shortcuts(protocol_project, monkeypatch):
    sql = render(protocol_project, monkeypatch, "maintenance_probe_steps",
                 "dbt_dqm.maintenance_steps(30,90)")
    # The fixture overrides outcome logging; absence of eligible rows cannot hide those hooks.
    assert sql.strip() == "[]"


@pytest.mark.parametrize("eligibility, kind, expected_jobs", [
    (False, "automatic", 2), (None, "automatic", 5),
    (True, "automatic", 5), (False, "manual", 4),
])
def test_maintenance_executor_skips_only_proven_empty_defaults(
    protocol_project, monkeypatch, tmp_path, eligibility, kind, expected_jobs
):
    import agate
    from dbt.adapters.bigquery.connections import BigQueryAdapterResponse

    original, profiles = protocol_project
    project = tmp_path / "maintenance"
    shutil.copytree(original, project, ignore=shutil.ignore_patterns("dbt_packages", "target", "logs"))
    (project / "macros/acceptance_protocol.sql").unlink()
    (project / "dbt_packages").mkdir()
    (project / "dbt_packages/dbt_dqm").symlink_to(ROOT, target_is_directory=True)
    scripts = []
    messages = []
    marker = "2026-10-10T00:00:00.000000Z"

    def execute(self, sql, **kwargs):
        scripts.append(sql)
        if "declare dqm_eligible_" in sql:
            table = agate.Table([(name, eligibility, marker) for name in
                                 ("staging_expiry", "stage_drops", "log_pruning")],
                                ["step", "eligible", "trigger_marker_at"],
                                column_types=[agate.Text(), agate.Boolean(), agate.Text()])
        elif "select dqm_step_ok as ok" in sql:
            table = agate.Table([(True, "job_1", marker)], ["ok", "job_id", "trigger_marker_at"],
                                column_types=[agate.Boolean(), agate.Text(), agate.Text()])
        else:
            table = agate.Table([], [])
        return BigQueryAdapterResponse(_message="OK"), table

    def closed(cls, connection):
        connection.state = ConnectionState.CLOSED
        return connection

    monkeypatch.setattr(BigQueryConnectionManager, "clear_transaction", lambda self: None)
    monkeypatch.setattr(BigQueryConnectionManager, "execute", execute)
    monkeypatch.setattr(BigQueryConnectionManager, "close", classmethod(closed))
    monkeypatch.setattr(BigQueryConnectionManager, "open", classmethod(
        lambda *args: (_ for _ in ()).throw(AssertionError("Live connection attempted"))))
    args = ["--no-populate-cache", "run-operation", "dbt_dqm.maintain_dqm",
            "--project-dir", str(project), "--profiles-dir", str(profiles),
            "--vars", "{dqm_enabled: true, dbt_dqm_maintenance_shortcuts: true}"]
    if kind == "manual":
        args[2] = "dbt_dqm.cleanup_dqm_logs"
        # Explicit cleanup verifies the logged outcomes after execution.
        def manual_execute(self, sql, **kwargs):
            if "select step, outcome, diagnostic_id" in sql:
                return BigQueryAdapterResponse(_message="OK"), agate.Table(
                    [(name, "succeeded", None) for name in ("staging_expiry", "stage_drops", "log_pruning")],
                    ["step", "outcome", "diagnostic_id"])
            return execute(self, sql, **kwargs)
        monkeypatch.setattr(BigQueryConnectionManager, "execute", manual_execute)
    result = dbtRunner(callbacks=[lambda event: messages.append(event.info.msg)]).invoke(args)
    assert result.success, result.exception
    assert len(scripts) == expected_jobs
    log = scripts[-1]
    assert log.count("insert into") == 3
    if eligibility is False and kind == "automatic":
        assert log.count("no eligible work") == 3
        assert marker in log
        observed = json.loads(next(msg.removeprefix("DQM_MAINTENANCE=") for msg in messages
                                   if msg.startswith("DQM_MAINTENANCE=")))
        assert {item["action"] for item in observed} == {"no_eligible_work"}
    else:
        assert "no eligible work" not in log


def test_bigquery_apply_fences_and_protects_annotations(protocol_project, monkeypatch):
    sql = render(protocol_project, monkeypatch, "apply_reconciliation")
    update = sql.split("when matched then update set", 1)[1].split("when not matched", 1)[0]
    for field in (
        "notes",
        "dqm_owner",
        "review_verdict",
        "annotation_version",
        "annotation_updated_at",
        "test_status",
    ):
        assert f"{field}=" not in update
    assert "then target.workflow_status else target.workflow_status_at_close" in update
    assert "run.generation_at_freeze=control.generation" in sql
    assert "run.status='started'" in sql
    assert "begin transaction" in sql and "commit transaction" in sql
    assert "dqm_reconciliation_receipts" in sql and "dqm_issue_events" in sql
    assert "drop table" not in sql
    assert "set generation=generation+1 where true;" in sql


def test_app_staging_migration_and_cleanup_render_without_raw_retention(protocol_project, monkeypatch):
    sql = render(protocol_project, monkeypatch, "setup_sql")
    assert "0004_app_staging_safety" in sql
    assert "alter column staged_at set default current_timestamp()" in sql
    assert "set staged_at=current_timestamp() where staged_at is null" in sql
    steps = render(protocol_project, monkeypatch, "maintenance_steps", "none, none")
    assert "staging_expiry" in steps and "raw_pruning" not in steps
    expiry = render(
        protocol_project,
        monkeypatch,
        "bigquery__maintenance_step_script",
        "{'name': 'staging_expiry', 'transactional': false}, none, none",
    )
    assert "dqm_app_change_staging" in expiry and "interval 24 hour" in expiry
    assert "delete from `compile-only`.`dqm`.`dqm_test_executions`" not in expiry


def step_script(protocol_project, monkeypatch, name, transactional, days="30"):
    return render(
        protocol_project,
        monkeypatch,
        "bigquery__maintenance_step_script",
        f"{{'name': '{name}', 'transactional': {str(transactional).lower()}}}, {days}, 365",
    )


def test_bigquery_maintenance_steps_report_outcomes_and_log_separately(
    protocol_project, monkeypatch
):
    raw = step_script(protocol_project, monkeypatch, "raw_pruning", True)
    # Each step job reports its own outcome; the log rows are written by a separate job.
    assert "select dqm_step_ok as ok, @@script.job_id as job_id," in raw
    assert raw.rstrip().endswith("as trigger_marker_at;")
    assert "dqm_maintenance_log" not in raw
    assert "begin transaction; set dqm_step_txn = true;" in raw
    assert "set dqm_step_txn = false; commit transaction;" in raw
    assert "if dqm_step_txn then\n      rollback transaction;" in raw
    log = render(
        protocol_project,
        monkeypatch,
        "bigquery__maintenance_log_script",
        "[{'step': 'raw_pruning', 'ok': false, 'job_id': 'job_1'}, "
        "{'step': 'log_pruning', 'ok': true, 'job_id': 'job_2'}], "
        "\"timestamp '2026-10-10T00:00:00.000000Z'\", 'automatic'",
    )
    # One job logs every outcome after the steps; each insert is protected on its own.
    assert log.count("insert into `compile-only`.`dqm`.`dqm_maintenance_log`") == 2
    assert log.count("exception when error then") == 2
    assert "trigger_marker_at,trigger_kind" in log and "'automatic'" in log
    # The first step's job reads the marker before its own work; later steps don't read it.
    first = render(
        protocol_project,
        monkeypatch,
        "bigquery__maintenance_step_script",
        "{'name': 'raw_pruning', 'transactional': true}, 30, 365, true",
    )
    assert first.index("set dqm_trigger_marker =") < first.index("begin transaction")
    assert "status='completed'" in first and "as trigger_marker_at;" in first
    assert "set dqm_trigger_marker =" not in raw


def test_bigquery_stage_drops_fail_the_step_but_recovery_stays_tolerant(
    protocol_project, monkeypatch
):
    drops = step_script(protocol_project, monkeypatch, "stage_drops", False)
    assert "set dqm_stage_failed = true;" in drops
    assert "raise using message = 'DQM stage drop failed'" in drops
    tolerant = render(protocol_project, monkeypatch, "cleanup_stages_sql")
    assert "Warning: DQM stage cleanup failed" in tolerant
    assert "raise" not in tolerant and "dqm_stage_failed" not in tolerant


def test_bigquery_run_pruning_keeps_runs_whose_stage_still_exists(protocol_project, monkeypatch):
    pruning = step_script(protocol_project, monkeypatch, "run_pruning", True)
    snapshot = pruning.index("create or replace temp table dqm_live_stages")
    assert "INFORMATION_SCHEMA.TABLES" in pruning
    # The stage list is read before the transaction opens; both deletes use it.
    assert snapshot < pruning.index("begin transaction")
    assert pruning.count("from dqm_live_stages live") == 2
    assert "concat('dqm_reconcile_stage_', replace(run.run_id, '-', ''))" in pruning


def test_bigquery_stage_is_created_by_the_freezing_job_with_an_expiry(
    protocol_project, monkeypatch
):
    # A run row becomes visible only when freeze commits. The stage DDL is in the same script, and
    # a script runs for at most 18 hours (with retries), so it always precedes the earliest
    # possible pruning of its run (at least a day after the run completes or is abandoned).
    sql = render(protocol_project, monkeypatch, "reconcile_pre")
    freeze = sql.index("commit transaction;")
    stage = re.search(r"create table .*dqm_reconcile_stage_[a-f0-9]+", sql).start()
    assert freeze < stage
    assert "expiration_timestamp=timestamp_add(current_timestamp(), interval 24 hour)" in sql


def test_bigquery_runtime_stages_and_manifest_stay_stable(protocol_project, monkeypatch):
    first = render(protocol_project, monkeypatch, "reconcile_pre")
    project, _ = protocol_project
    manifest1 = json.loads((project / "target/manifest.json").read_text())
    second = render(protocol_project, monkeypatch, "reconcile_pre")
    manifest2 = json.loads((project / "target/manifest.json").read_text())
    stage1 = re.search(r"create table .*dqm_reconcile_stage_([a-f0-9]+)", first).group(1)
    stage2 = re.search(r"create table .*dqm_reconcile_stage_([a-f0-9]+)", second).group(1)
    assert stage1 != stage2
    for manifest in (manifest1, manifest2):
        node = manifest["nodes"]["model.dbt_dqm.dqm_reconcile"]
        assert node["alias"] == "dqm_reconcile" and node["config"]["materialized"] == "view"
        assert stage1 not in json.dumps(node["config"]) and stage2 not in json.dumps(node["config"])
    assert "create table if not exists `compile-only`.`dqm`.`dqm_install` as select" in first
    assert "dqm_migration_token" in first and "migration_lease_until" in first
    assert "interval 60 minute), generation=generation+1 where true;" in first
    assert "Late DQM evidence" in first
    assert "dqm_reconciliation_inputs" in first


def test_duplicate_late_skip_is_rejected_before_warehouse_access(
    protocol_project, monkeypatch, capsys
):
    project, profiles = protocol_project

    def closed(cls, connection):
        connection.state = ConnectionState.CLOSED
        return connection

    def reject(*args, **kwargs):
        raise AssertionError("Duplicate skip attempted warehouse access")

    monkeypatch.setattr(BigQueryConnectionManager, "close", classmethod(closed))
    monkeypatch.setattr(BigQueryConnectionManager, "open", classmethod(reject))
    item = {"test_unique_id": "test.example.check", "invocation_id": "original-run"}
    result = dbtRunner().invoke(
        [
            "run-operation",
            "dqm_skip_late_evidence",
            "--args",
            json.dumps({"items": [item, item], "reason": "Reviewed delayed evidence"}),
            "--project-dir",
            str(project),
            "--profiles-dir",
            str(profiles),
        ]
    )
    assert not result.success
    assert "Late-evidence items must be distinct" in capsys.readouterr().out


def test_bigquery_native_grants_quote_roles_and_principals(protocol_project, monkeypatch):
    sql = render(
        protocol_project,
        monkeypatch,
        "native_grants_sql",
        "dbt_dqm.dqm_relation('dqm_issue_occurrences'), "
        "{'roles/bigquery.dataViewer': ['serviceAccount:demo@example.iam.gserviceaccount.com']}",
    )
    assert "grant `roles/bigquery.dataViewer` on TABLE" in sql
    assert "'serviceAccount:demo@example.iam.gserviceaccount.com'" in sql
    assert "revoke" not in sql.lower()


def test_dbt_dqm_schema_places_every_package_model(protocol_project):
    # A var in the package's own dbt_project.yml renders without the consumer's vars, so the
    # schema is set in each model; hook tables follow dqm_reconcile's schema (dqm_relation).
    project, profiles = protocol_project

    def schemas(*args):
        result = dbtRunner().invoke(
            [
                "ls",
                "--select",
                "package:dbt_dqm",
                "--resource-type",
                "model",
                "--output",
                "json",
                "--output-keys",
                "name schema",
                "--project-dir",
                str(project),
                "--profiles-dir",
                str(profiles),
                "--vars", "{dqm_enabled: true}",
                *args,
            ]
        )
        assert result.success, result.exception
        return {json.loads(line)["name"]: json.loads(line)["schema"] for line in result.result}

    assert set(schemas().values()) == {"dqm"}
    selected = schemas("--vars", "{dbt_dqm_schema: governance, dqm_enabled: true}")
    assert len(selected) == len(list((ROOT / "models").glob("*.sql")))
    assert set(selected.values()) == {"dqm_governance"}


def test_capture_summary_is_valid_bigquery_script_without_empty_statements(protocol_project, monkeypatch):
    """A real dbt render catches delimiters that PostgreSQL accepts but BigQuery rejects."""
    import agate
    import sqlglot
    from dbt.adapters.bigquery.connections import BigQueryAdapterResponse
    from dbt.adapters.bigquery.impl import BigQueryAdapter

    project, profiles = protocol_project
    scripts = []

    def execute(self, sql, **kwargs):
        scripts.append(sql)
        return BigQueryAdapterResponse(_message="OK"), agate.Table([], [])

    def closed(cls, connection):
        connection.state = ConnectionState.CLOSED
        return connection

    def reject(*args, **kwargs):
        raise AssertionError("Script rendering attempted warehouse access")

    # Already-initialized collection must not even render setup's metadata lookups.
    monkeypatch.setattr(BigQueryAdapter, "get_relation", reject)
    monkeypatch.setattr(BigQueryConnectionManager, "clear_transaction", lambda self: None)
    monkeypatch.setattr(BigQueryConnectionManager, "execute", execute)
    monkeypatch.setattr(BigQueryConnectionManager, "close", classmethod(closed))
    monkeypatch.setattr(BigQueryConnectionManager, "open", classmethod(reject))
    payload = {"invocation_id": "test-invocation", "results": [{
        "unique_id": "test.dbt_dqm_demo.demo_customer_email_invalid", "status": "error",
        "failures": None, "execution_time": 0, "message": "test execution failed"}]}
    result = dbtRunner().invoke([
        "--no-populate-cache", "run-operation", "dbt_dqm.collect_results", "--args",
        json.dumps({"payload": payload}), "--project-dir", str(project),
        "--profiles-dir", str(profiles), "--vars", "{dqm_enabled: true}"])
    assert result.success, result.exception
    assert len(scripts) == 1
    parsed = sqlglot.parse(scripts[0], read="bigquery")
    assert parsed and all(statement is not None for statement in parsed)
    assert "inconclusive" in scripts[0] and "captured_issue_count" in scripts[0]


def test_namespace_contract_covers_state_and_cleanup(protocol_project, monkeypatch):
    project,_profiles=protocol_project
    macro=project/'macros/a7_namespace.sql'
    macro.write_text('''{% macro generate_alias_name(custom_alias_name=none,node=none) %}
{{ ('alice_' if node.package_name=='dbt_dqm' else '') ~ (custom_alias_name|trim if custom_alias_name else node.name) }}
{% endmacro %}''')
    try:
        names=render(protocol_project,monkeypatch,'relation_contract')
        assert 'alice_dqm_issue_occurrences' in names and 'alice_dqm_install' in names
        sql=render(protocol_project,monkeypatch,'cleanup_stages_sql')
        assert 'alice_dqm_reconciliation_runs' in sql
        assert 'alice_dqm_reconcile_stage_' in sql and 'stage.stage_relation =' in sql
        assert 'strpos(' not in sql
        sql=render(protocol_project,monkeypatch,'default__app_staging_safety_apply')
        assert "table_name='alice_dqm_app_change_staging'" in sql
        sql=render(protocol_project,monkeypatch,'maintenance_eligible_sql',"{'name':'stage_drops'},30,90")
        assert 'dqm_runs.run_id' in sql and 'alice_dqm_reconciliation_runs' in sql
    finally:
        macro.unlink()


def test_reported_alias_precedence_and_environment_targets(protocol_project,monkeypatch):
    import importlib.util
    source=ROOT/'integration_tests/acceptance/namespace_checks.py'
    spec=importlib.util.spec_from_file_location('a7_namespace_cases',source)
    cases=importlib.util.module_from_spec(spec);spec.loader.exec_module(cases)
    project,profiles=protocol_project
    macro=project/'macros/a7_alias_precedence.sql';macro.write_text(cases.USER_ALIAS_MACRO)
    profile_path=profiles/'profiles.yml';original=profile_path.read_text()
    profile=yaml.safe_load(original);settings=profile['dbt_dqm_demo']
    output=settings['outputs']['dev']
    for target in ['prod_dev','dw','qa','ple','prod']:
        settings['outputs'][target]=dict(output)
    try:
        for target,variables,environment,expected in [
            ('dev',{'table_alias_prefix':'chosen'},{'DBT_TABLE_ALIAS_PREFIX':'env','USERNAME':'user'},'chosen_'),
            ('dev',{}, {'DBT_TABLE_ALIAS_PREFIX':'env','USERNAME':'user'},'env_'),
            ('dev',{}, {'DBT_TABLE_ALIAS_PREFIX':'','USERNAME':'user'},'user_'),
            ('dev',{}, {'DBT_TABLE_ALIAS_PREFIX':'','USERNAME':''},'dev_'),
            ('prod_dev',{'table_alias_prefix':'chosen'}, {},'chosen_'),
            ('dw',{'table_alias_prefix':'chosen'}, {},'chosen_'),
            ('qa',{'table_alias_prefix':'chosen'}, {},'qa_'),
            ('ple',{'table_alias_prefix':'chosen'}, {},''),
            ('prod',{'table_alias_prefix':'chosen'}, {},'')]:
            settings['target']=target;profile_path.write_text(yaml.safe_dump(profile))
            for key in ('DBT_TABLE_ALIAS_PREFIX','USERNAME'):
                monkeypatch.setenv(key,environment.get(key,''))
            sql=render(protocol_project,monkeypatch,'internal_alias',"'dqm_issue_events'",variables)
            assert sql.strip()==expected+'dqm_issue_events'
    finally:
        macro.unlink();profile_path.write_text(original)
