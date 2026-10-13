"""Consumer naming overrides and the app's physical relation contract."""

import json
import uuid
from datetime import UTC, datetime

from dbt_dqm_app.config import load_config
from dbt_dqm_app.store import Patch
from dbt_dqm_app.warehouse import (
    _postgres_connection,
    _relation_parts,
    apply_patches,
    fetch_issues,
    insert_missed_issue,
    sync_worker,
)

NAMING_MACROS = """
{% macro generate_alias_name(custom_alias_name=none, node=none) %}
{% set base = custom_alias_name | trim if custom_alias_name else node.name %}
{{ ('reviewer_' ~ base) if node.package_name == 'dbt_dqm' else base }}
{% endmacro %}
{% macro generate_schema_name(custom_schema_name, node) %}
{{ target.schema }}
{% endmacro %}
"""


def check_alias_app(project, profiles):
    config = load_config(project, profiles, "dev")
    snapshot = sync_worker(config)
    assert len(snapshot["issues"]) == 7
    assert "tests" in snapshot["health"]
    for name in ("dqm_all_issues", "dqm_annotation_changes", "dqm_missed_issues"):
        assert _relation_parts(config, name)[2] == "reviewer_" + name
    assert _relation_parts(config, "dqm_issue_occurrences")[2] == "dqm_issue_occurrences"
    nodes = json.loads((project / "target/manifest.json").read_text())["nodes"]
    assert "model.dbt_dqm.dqm_annotation_changes" in nodes[
        "model.dbt_dqm.dqm_missed_issues"]["depends_on"]["nodes"]
    assert "model.dbt_dqm.dqm_missed_issues" in nodes[
        "model.dbt_dqm.dqm_reconcile"]["depends_on"]["nodes"]
    issue = snapshot["issues"][0]
    patch = Patch(issue["occurrence_id"], "notes", issue.get("notes"),
                  "alias acceptance note", 1, datetime.now(UTC).isoformat(),
                  issue.get("annotation_version", 0))
    assert apply_patches(config, [patch])
    refreshed = fetch_issues(config)
    assert next(row for row in refreshed if row["occurrence_id"] == issue["occurrence_id"])[
        "notes"] == "alias acceptance note"
    if config.adapter_type == "postgres":
        with _postgres_connection(config) as connection, connection.cursor() as cursor:
            for name in ("dqm_annotation_changes", "dqm_missed_issues"):
                _, schema, identifier = _relation_parts(config, name)
                cursor.execute("select count(*) from pg_indexes where schemaname=%s "
                               "and tablename=%s and indexdef like 'CREATE UNIQUE INDEX%%'",
                               (schema, identifier))
                assert cursor.fetchone()[0] >= 1, f"{identifier} has no unique index"
    identity = uuid.uuid4().hex
    assert insert_missed_issue(config, {
        "missed_issue_id": identity, "description": "alias acceptance report",
        "area_label": "synthetic alias QA", "root_cause": "no_test",
    }) == identity
