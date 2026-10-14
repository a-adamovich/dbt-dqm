"""Existing datasets must not require project-level dataset-create permission."""
from pathlib import Path
from types import SimpleNamespace

import jinja2
import pytest


@pytest.mark.parametrize("exists", [True, False])
def test_initialize_creates_only_a_missing_schema(exists):
    calls = []
    anchor = SimpleNamespace(database="project", schema="dataset")

    def check_schema(database, schema):
        calls.append(("check", database, schema))
        return exists

    adapter = SimpleNamespace(check_schema_exists=check_schema,
                              create_schema=lambda relation: calls.append(("create", relation)))
    package = SimpleNamespace(dqm_relation=lambda name: anchor, setup_sql=lambda: "select 1;")
    environment = jinja2.Environment(extensions=["jinja2.ext.do"])
    source = (Path(__file__).parents[1] / "macros/workflow.sql").read_text()
    environment.from_string(source + "{{ initialize_dqm() }}").render(
        execute=True, var=lambda name, default: True,
        adapter=adapter, dbt_dqm=package, target=SimpleNamespace(type="bigquery"),
        run_query=lambda sql: calls.append(("query", sql.strip())),
    )
    expected = [("check", "project", "dataset")]
    if not exists:
        expected.append(("create", anchor))
    assert calls == [*expected, ("query", "select 1;")]
