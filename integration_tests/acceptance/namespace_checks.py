"""Two developer namespaces sharing one disposable dataset, using the reported naming macro."""

import json
from datetime import UTC, datetime

import pytest

from dbt_dqm_app.config import load_config
from dbt_dqm_app.errors import AppError
from dbt_dqm_app.exports import write_export
from dbt_dqm_app.relations import resolve_config
from dbt_dqm_app.store import Patch
from dbt_dqm_app.summary import owner_summary
from dbt_dqm_app.warehouse import _query_rows, _table, apply_patches, fetch_issues

USER_ALIAS_MACRO = """
{% macro generate_alias_name(custom_alias_name=none,node=none) %}
{% if target.name in ['dev','prod_dev','dw'] %}
  {% set value=var('table_alias_prefix','') or env_var('DBT_TABLE_ALIAS_PREFIX','') or env_var('USERNAME','') %}
  {% set prefix=(value ~ '_') if value else ((target.user ~ '_') if target.user else 'dev_') %}
{% elif target.name=='qa' %}{% set prefix='qa_' %}
{% else %}{% set prefix='' %}{% endif %}
{% if custom_alias_name %}{{ prefix }}{{ custom_alias_name|trim }}
{% elif node.version %}{{ prefix }}{{ node.name ~ '_v' ~ (node.version|string|replace('.','_')) }}
{% else %}{{ prefix }}{{ node.name }}{% endif %}
{% endmacro %}
"""


def check_namespace_isolation(demo, dedicated):
    project = getattr(demo, "project", None) or demo.path
    # Establish legacy data, then verify that an upgrade cannot silently abandon it.
    demo.dbt("run", "--select", "demo_records")
    dedicated(demo)
    canonical = resolve_config(load_config(project, demo.profiles, "dev"))
    baseline = _query_rows(
        canonical,
        f"select * from {_table(canonical, 'dqm_issue_occurrences')} order by occurrence_id",
    )
    (project / "macros/namespace_alias.sql").write_text(USER_ALIAS_MACRO)
    if canonical.adapter_type == "postgres":
        refused = dedicated(demo, variables={"table_alias_prefix": "ü" * 25}, code=2)
        assert "63-byte limit" in refused.stderr
        assert "DQM: test" not in refused.stdout
    configs = {}
    for prefix in ("alice", "bob"):
        variables = {"table_alias_prefix": prefix}
        demo.dbt("seed", variables=variables)
        demo.dbt("run", "--select", "demo_records", variables=variables)
        refused = dedicated(demo, variables=variables, code=2)
        assert "Legacy shared" in refused.stderr
        assert (
            _query_rows(
                canonical,
                f"select * from {_table(canonical, 'dqm_issue_occurrences')} order by occurrence_id",
            )
            == baseline
        )
        result = dedicated(demo, "--full-refresh", "--summary", variables=variables)
        assert "DQM owner summary" in result.stdout
        assert result.stdout.count("DQM test warn:") == 3
        assert "WARN 3 in" not in result.stdout and "[WARNING]: in test" not in result.stdout
        config = resolve_config(load_config(project, demo.profiles, "dev", cli_vars=variables))
        assert all(row[3].startswith(prefix + "_") for row in config.relations)
        assert len(fetch_issues(config)) == 7
        configs[prefix] = config
    alice, bob = configs["alice"], configs["bob"]
    assert alice.workspace_path != bob.workspace_path
    issue = fetch_issues(bob)[0]
    patch = Patch(
        issue["occurrence_id"],
        "workflow_status",
        issue["workflow_status"],
        "TRIAGED",
        1,
        datetime.now(UTC).isoformat(),
        issue["annotation_version"],
        installation_id=issue["_installation_id"],
    )
    assert apply_patches(bob, [patch])
    with pytest.raises(AppError, match="installation"):
        apply_patches(alice, [patch])
    from review_checks import check_review_actions

    check_review_actions(bob)
    expected = fetch_issues(bob)
    # Both export formats remain readable under the fully resolved naming contract.
    for format in ("csv", "xlsx"):
        exported = write_export(iter(expected), project / ("isolated-export-" + format), format)
        assert exported.rows == 7
    if bob.adapter_type == "bigquery":
        from google.cloud import bigquery

        from dbt_dqm_app.warehouse import client_for

        live = _query_rows(
            alice,
            f"select stage_relation from {_table(alice, 'dqm_reconciliation_runs')} "
            "where status='completed' order by completed_at desc limit 1",
        )[0]["stage_relation"]
        client = client_for(bob)
        client.query(
            f"update {_table(bob, 'dqm_reconciliation_runs')} set status='abandoned', "
            "stage_relation=@foreign_stage where status='completed'",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("foreign_stage", "STRING", live)]
            ),
        ).result()
        demo.dbt(
            "run-operation", "dbt_dqm.dqm_cleanup_stages", variables={"table_alias_prefix": "bob"}
        )
        assert client.get_table(live.replace("`", ""))
    result = dedicated(
        demo, "--full-refresh", "--summary", variables={"table_alias_prefix": "alice"}
    )
    assert "alice_dqm_issue_occurrences" in result.stdout
    assert "bob_dqm_issue_occurrences" not in result.stdout
    assert sorted(fetch_issues(bob), key=lambda row: row["occurrence_id"]) == sorted(
        expected, key=lambda row: row["occurrence_id"]
    )
    assert (
        _query_rows(
            canonical,
            f"select * from {_table(canonical, 'dqm_issue_occurrences')} order by occurrence_id",
        )
        == baseline
    )
    artifact = json.loads((project / "target/dqm_model_run_results.json").read_text())
    summary = owner_summary(alice, artifact["metadata"]["invocation_id"])
    assert summary["active"] == summary["new"] == 7
