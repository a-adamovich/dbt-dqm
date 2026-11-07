"""Real 2.0.9 smoke tests; CI supplies an isolated, user-installed engine executable."""

import json
import os
import shutil
from pathlib import Path

import pytest
import yaml

from dbt_dqm_app.config import load_config
from dbt_dqm_app.dbt_cli import DbtCLI
from dbt_dqm_app.relations import resolve_config
from dbt_dqm_app.runtime_project import test_project as projected_project

ROOT = Path(__file__).resolve().parents[1]
EXECUTABLE = os.environ.get("DBT_DQM_V2_EXECUTABLE")
pytestmark = pytest.mark.skipif(not EXECUTABLE, reason="Set DBT_DQM_V2_EXECUTABLE to dbt 2.0.9")


@pytest.fixture
def project(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    for name in ("macros", "models", "tests"):
        shutil.copytree(ROOT / name, package / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.py"))
    # Exercise the candidate before widening the published compatibility constraint.
    config = yaml.safe_load((ROOT / "dbt_project.yml").read_text())
    config["require-dbt-version"] = [">=1.11.0", "<3.0.0"]
    (package / "dbt_project.yml").write_text(yaml.safe_dump(config))
    consumer = tmp_path / "project with spaces"
    consumer.mkdir()
    (consumer / "dbt_packages").mkdir()
    (consumer / "dbt_packages/dbt_dqm").symlink_to(package, target_is_directory=True)
    (consumer / "packages.yml").write_text(yaml.safe_dump({"packages": [{"local": str(package)}]}))
    (consumer / "dbt_project.yml").write_text("name: compat\nprofile: compat\nconfig-version: 2\n")
    (consumer / "profiles.yml").write_text(yaml.safe_dump({"compat": {"target": "dev", "outputs": {
        "dev": {"type": "bigquery", "method": "oauth", "project": "dbt-dqm-compile-only",
                "dataset": "compat", "location": "US", "threads": 1}}}}))
    (consumer / "tests").mkdir()
    (consumer / "tests/marked.sql").write_text("""{{ config(enabled=var('dqm_enabled', false),
        store_failures=true, meta={'dbt_dqm': {'granularity': ['id']}}) }}
        select 1 as id where false
    """)
    return consumer


def test_v2_parse_select_and_compile_without_python_adapters(project):
    runner = DbtCLI(executable=EXECUTABLE)
    runner.validate("bigquery")
    assert str(runner.version) == "2.0.9"
    flags = ["--project-dir", str(project), "--profiles-dir", str(project),
             "--target-path", str(project / "target"), "--log-path", str(project / "logs"),
             "--vars", "{dqm_enabled: true, _dbt_dqm_workflow: true}"]
    parsed = runner.invoke(["parse", *flags])
    assert parsed.success, parsed.exception
    assert "model.dbt_dqm.dqm_reconcile" in parsed.result.nodes
    selected = runner.invoke(["ls", "--resource-type", "test", "--select", "marked",
                              "--output", "json", "--output-keys", "unique_id", "fqn", *flags])
    assert selected.success, selected.exception
    assert [json.loads(row)["unique_id"] for row in selected.result] == ["test.compat.marked"]
    compiled = runner.invoke(["compile", "--no-introspect", "--select", "marked", *flags])
    assert compiled.success, compiled.exception
    assert "select 1 as id" in compiled.result.nodes["test.compat.marked"].compiled_code


def test_v2_app_naming_isolated_from_consumer_artifacts(project):
    project_file = project / "dbt_project.yml"
    document = yaml.safe_load(project_file.read_text())
    document["on-run-end"] = ["{{ compat.noop() }}"]
    project_file.write_text(yaml.safe_dump(document))
    (project / "macros").mkdir()
    (project / "macros/noop.sql").write_text("{% macro noop() %}{% endmacro %}")
    target = project / "target"
    target.mkdir()
    artifact = target / "manifest.json"
    artifact.write_text('{"consumer": true}')
    config = load_config(project, project, dbt_executable=EXECUTABLE)
    resolved = resolve_config(config)
    assert resolved.relations and dict(resolved.test_granularity)["test.compat.marked"] == ("id",)
    assert artifact.read_text() == '{"consumer": true}'


def test_v2_root_override_preserves_test_identity_and_source_paths(project):
    runner = DbtCLI(executable=EXECUTABLE)
    flags = ["--profiles-dir", str(project), "--target-path", str(project / "target"),
             "--log-path", str(project / "logs"), "--vars", "{dqm_enabled: true}"]
    original = runner.invoke(["parse", "--project-dir", str(project), *flags])
    assert original.success, original.exception
    before = original.result.nodes["test.compat.marked"]
    with projected_project(project, {}) as runtime:
        result = runner.invoke(["parse", "--project-dir", str(runtime), *flags])
        assert result.success, result.exception
        after = result.result.nodes["test.compat.marked"]
        assert (after.unique_id, after.fqn, after.original_file_path) == (
            before.unique_id, before.fqn, before.original_file_path)
        assert "macro.compat.materialization_test_default" in result.result.macros
    assert not list(project.glob("__dbt_dqm_runtime*"))


def test_v2_isolates_an_existing_root_test_materialization(project):
    macros = project / "macros"
    macros.mkdir()
    source = """{% materialization test, default %}
      {{ return(dbt.materialization_test_default()) }}
    {% endmaterialization %}
    {% macro another_macro() %}select 1{% endmacro %}
    """
    (macros / "custom_test.sql").write_text(source)
    runner = DbtCLI(executable=EXECUTABLE)
    flags = ["--profiles-dir", str(project), "--target-path", str(project / "target"),
             "--log-path", str(project / "logs"), "--vars", "{dqm_enabled: true}"]
    with projected_project(project, {}) as runtime:
        result = runner.invoke(["parse", "--project-dir", str(runtime), *flags])
        assert result.success, result.exception
        assert "macro.compat.materialization_test_default" in result.result.macros
        assert "macro.compat.another_macro" in result.result.macros
    assert (macros / "custom_test.sql").read_text() == source


def test_v2_workflow_compile_does_not_execute_consumer_or_package_global_hooks(project):
    root_config = project / "dbt_project.yml"
    config = yaml.safe_load(root_config.read_text())
    config["on-run-start"] = ["{{ compat.fail_if_executed() }}"]
    root_config.write_text(yaml.safe_dump(config))
    (project / "macros").mkdir()
    (project / "macros/hooks.sql").write_text("""{% macro fail_if_executed() %}
      {% if execute %}{{ exceptions.raise_compiler_error('consumer hook ran during compile') }}{% endif %}
    {% endmacro %}""")
    package = project.parent / "package"
    package_config = yaml.safe_load((package / "dbt_project.yml").read_text())
    package_config["on-run-end"] = ["{{ dbt_dqm.fail_if_executed() }}"]
    (package / "dbt_project.yml").write_text(yaml.safe_dump(package_config))
    (package / "macros/compat_hook.sql").write_text("""{% macro fail_if_executed() %}
      {% if execute %}{{ exceptions.raise_compiler_error('package hook ran during compile') }}{% endif %}
    {% endmacro %}""")
    runner = DbtCLI(executable=EXECUTABLE)
    arguments = ["compile", "--no-introspect", "--select", "marked", "--project-dir", str(project),
                 "--profiles-dir", str(project), "--target-path", str(project / "target"),
                 "--log-path", str(project / "logs"),
                 "--vars", "{dqm_enabled: true, _dbt_dqm_workflow: true}"]
    compiled = runner.invoke(arguments)
    assert compiled.success, compiled.exception
    assert "select 1 as id" in compiled.result.nodes["test.compat.marked"].compiled_code
    assert "on-run-start" in yaml.safe_load(root_config.read_text())
    assert "on-run-end" in yaml.safe_load((package / "dbt_project.yml").read_text())


def test_v2_compiles_package_models_without_warehouse_credentials(project):
    events = []
    runner = DbtCLI(executable=EXECUTABLE, callbacks=[events.append])
    arguments = ["compile", "--no-introspect", "--select", "package:dbt_dqm",
                 "--project-dir", str(project), "--profiles-dir", str(project),
                 "--target-path", str(project / "target"), "--log-path", str(project / "logs"),
                 "--vars", "{dqm_enabled: true, _dbt_dqm_workflow: true}"]
    result = runner.invoke(arguments)
    errors = [event.info.msg for event in events if event.info.level == "error"]
    assert result.success, result.exception or "\n".join(errors)
    assert result.result.nodes["model.dbt_dqm.dqm_reconcile"].compiled_code


def test_v2_maintenance_probe_conservatively_runs_steps_without_dispatch_metadata(project):
    (project / "macros").mkdir()
    (project / "macros/probe.sql").write_text("""{% macro probe_maintenance() %}
      {% set steps = [{'name': 'event_retention'}] %}
      {{ log('PROBE_COUNT=' ~ (dbt_dqm.maintenance_probe_steps(steps) | length), info=true) }}
    {% endmacro %}""")
    events = []
    runner = DbtCLI(executable=EXECUTABLE, callbacks=[events.append])
    result = runner.invoke(["run-operation", "probe_maintenance", "--project-dir", str(project),
                            "--profiles-dir", str(project), "--target-path", str(project / "target"),
                            "--log-path", str(project / "logs")])
    assert result.success, result.exception
    assert any("PROBE_COUNT=0" in event.info.msg for event in events)


def test_v2_supports_the_capture_macro_regex_context(project):
    events = []
    runner = DbtCLI(executable=EXECUTABLE, callbacks=[events.append])
    arguments = ["compile", "--no-introspect", "--inline",
                 "{{ modules.re.sub('\\\\s+', '', 'count( * )') }}", "--project-dir", str(project),
                 "--profiles-dir", str(project), "--target-path", str(project / "target"),
                 "--log-path", str(project / "logs"), "--vars", "{dqm_enabled: true}"]
    result = runner.invoke(arguments)
    errors = [event.info.msg for event in events if event.info.level == "error"]
    assert result.success, result.exception or "\n".join(errors)


def test_v2_supports_the_maintenance_elapsed_time_context(project):
    events = []
    runner = DbtCLI(executable=EXECUTABLE, callbacks=[events.append])
    source = ("{% set start = modules.datetime.datetime.now() %}"
              "select {{ (modules.datetime.datetime.now() - start).total_seconds() }}")
    arguments = ["compile", "--no-introspect", "--inline", source, "--project-dir", str(project),
                 "--profiles-dir", str(project), "--target-path", str(project / "target"),
                 "--log-path", str(project / "logs"), "--vars", "{dqm_enabled: true}"]
    result = runner.invoke(arguments)
    errors = [event.info.msg for event in events if event.info.level == "error"]
    assert result.success, result.exception or "\n".join(errors)


def test_v2_resolves_every_tracking_schema_column_type(project):
    events = []
    runner = DbtCLI(executable=EXECUTABLE, callbacks=[events.append])
    source = ("select {% for kind in ['string','timestamp','int','bigint','float'] %}"
              "cast(null as {{ dbt_dqm.column_type(kind) }}) as {{ kind }}"
              "{% if not loop.last %}, {% endif %}{% endfor %}")
    arguments = ["compile", "--no-introspect", "--inline", source, "--project-dir", str(project),
                 "--profiles-dir", str(project), "--target-path", str(project / "target"),
                 "--log-path", str(project / "logs"), "--vars", "{dqm_enabled: true}"]
    result = runner.invoke(arguments)
    errors = [event.info.msg for event in events if event.info.level == "error"]
    assert result.success, result.exception or "\n".join(errors)


def test_v2_dispatches_a_migration_name_supplied_at_runtime(project):
    (project / "macros").mkdir()
    (project / "macros/dynamic_dispatch.sql").write_text("""
{% macro default__compat_probe() %}{{ return('ok') }}{% endmacro %}
{% macro compat_dispatch_test() %}
  {% set spec = {'apply': 'compat_probe'} %}
  {% set migration_adapter = adapter %}
  {% do log('DQM_DISPATCH_TEST=' ~ migration_adapter.dispatch(spec.apply, 'compat')(), info=true) %}
{% endmacro %}
""")
    events = []
    runner = DbtCLI(executable=EXECUTABLE, callbacks=[events.append])
    arguments = ["run-operation", "compat_dispatch_test", "--project-dir", str(project),
                 "--profiles-dir", str(project), "--target-path", str(project / "target"),
                 "--log-path", str(project / "logs"), "--vars", "{dqm_enabled: true}"]
    result = runner.invoke(arguments)
    errors = [event.info.msg for event in events if event.info.level == "error"]
    assert result.success, result.exception or "\n".join(errors)
    assert any(event.info.msg == "DQM_DISPATCH_TEST=ok" for event in events)
