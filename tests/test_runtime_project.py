"""The explicit materialization override must not modify consumer files or resource paths."""

from pathlib import Path
from types import SimpleNamespace

import yaml

from dbt_dqm_app.runtime_project import test_project as projected_project


def test_projection_keeps_sources_packages_and_original_config_unchanged(tmp_path):
    project = tmp_path / "consumer with spaces"
    project.mkdir()
    for name in ("macros", "tests", "models", "dbt_packages"):
        (project / name).mkdir()
    (project / "tests/check.sql").write_text("select id from {{ ref('records') }}")
    (project / "packages.yml").write_text("packages:\n  - local: ../dependency\n")
    original = "name: consumer\nprofile: consumer\nmacro-paths: [macros]\n"
    (project / "dbt_project.yml").write_text(original)
    with projected_project(project, {}) as runtime:
        config = yaml.safe_load((runtime / "dbt_project.yml").read_text())
        assert config["name"] == "consumer" and config["macro-paths"][0] == "macros"
        wrapper = runtime / config["macro-paths"][-1] / "test.sql"
        assert "dbt_dqm.materialization_test_default()" in wrapper.read_text()
        assert (runtime / "tests/check.sql").read_text() == (project / "tests/check.sql").read_text()
        assert (runtime / "dbt_packages").resolve() == project / "dbt_packages"
        assert yaml.safe_load((runtime / "packages.yml").read_text())["packages"][0]["local"] == str(tmp_path / "dependency")
        assert (project / "dbt_project.yml").read_text() == original
        assert list((project / "macros").iterdir()) == []
    assert not runtime.exists()


def test_projection_resolves_external_resource_paths_against_consumer(tmp_path):
    project = tmp_path / "consumer"
    project.mkdir()
    (project / "dbt_project.yml").write_text(yaml.safe_dump({
        "name": "consumer", "profile": "consumer", "test-paths": ["../shared_tests"],
        "packages-install-path": "{{ var('packages_path') }}", "macro-paths": ["macros"],
    }))
    with projected_project(project, {"packages_path": "../shared_packages"}) as runtime:
        config = yaml.safe_load((runtime / "dbt_project.yml").read_text())
        assert config["test-paths"] == [str(tmp_path / "shared_tests")]
        assert config["packages-install-path"] == str(tmp_path / "shared_packages")
        assert Path(config["packages-install-path"]).is_absolute()


def test_compile_projection_omits_global_hooks_without_changing_sources(tmp_path):
    project = tmp_path / "consumer"
    package = project / "dbt_packages/dependency"
    package.mkdir(parents=True)
    (project / "dbt_project.yml").write_text(yaml.safe_dump({
        "name": "consumer", "profile": "consumer", "on-run-start": ["select 1"],
    }))
    original = yaml.safe_dump({"name": "dependency", "on-run-end": ["select 2"]})
    (package / "dbt_project.yml").write_text(original)
    (package / "macros").mkdir()
    with projected_project(project, {}, compile_only=True) as runtime:
        config = yaml.safe_load((runtime / "dbt_project.yml").read_text())
        copied = Path(config["packages-install-path"]) / "dependency"
        assert "on-run-start" not in config
        assert "on-run-end" not in yaml.safe_load((copied / "dbt_project.yml").read_text())
        assert (copied / "macros").resolve() == package / "macros"
        assert (package / "dbt_project.yml").read_text() == original


def test_projection_preserves_other_macros_when_consumer_overrides_native_test(tmp_path):
    project = tmp_path / "consumer"
    project.mkdir()
    (project / "dbt_project.yml").write_text("name: consumer\nprofile: consumer\n")
    macros = project / "macros"
    macros.mkdir()
    original = """{% materialization test, default %}
{{ return(dbt.materialization_test_default()) }}
{% endmaterialization %}
{% macro another_macro() %}select 1{% endmacro %}
"""
    (macros / "test.sql").write_text(original)
    with projected_project(project, {}) as runtime:
        paths = yaml.safe_load((runtime / "dbt_project.yml").read_text())["macro-paths"]
        isolated = Path(paths[0]) / "test.sql"
        source = isolated.read_text()
        assert "{% materialization test" not in source
        assert "{% macro another_macro() %}" in source
        assert (runtime / paths[-1] / "test.sql").exists()
        assert (macros / "test.sql").read_text() == original


def test_windows_projection_copies_without_privilege_and_keeps_originals(tmp_path, monkeypatch):
    import json

    from dbt_dqm_app import runtime_project

    project = tmp_path / 'consumer unicode é'
    package = project / 'dbt_packages/dependency'
    (package / 'macros').mkdir(parents=True)
    (package / 'dbt_project.yml').write_text('name: dependency\non-run-start: [select 1]\n')
    (project / 'dbt_project.yml').write_text('name: consumer\nvars: {user_value: kept}\n')
    (project / 'models').mkdir()
    original = "select '{{ literal }}' as id"
    (project / 'models/example.sql').write_text(original)
    def denied(*args, **kwargs):
        error = OSError('symlink denied')
        error.winerror = 1314
        raise error
    monkeypatch.setattr(Path, 'symlink_to', denied)
    monkeypatch.setattr(runtime_project, 'sys', SimpleNamespace(platform='win32'))
    payload = {'validation': {'compiled_queries': {'test.large': original * 20000}}}
    for compile_only in (False, True):
        with projected_project(project, {}, compile_only=compile_only, project_vars=payload) as root:
            config = yaml.safe_load((root / 'dbt_project.yml').read_text())
            assert config['vars']['user_value'] == 'kept'
            assert '{{' not in config['vars']['_dbt_dqm_internal_json']
            assert json.loads(config['vars']['_dbt_dqm_internal_json']) == payload
            assert (root / 'models/example.sql').read_text() == original
        assert not root.exists()
        assert (project / 'models/example.sql').read_text() == original
        assert (package / 'dbt_project.yml').exists()


def test_projection_does_not_hide_unrelated_filesystem_errors(tmp_path, monkeypatch):
    import pytest

    from dbt_dqm_app.runtime_project import _project_path

    def denied(*args, **kwargs):
        raise PermissionError('unrelated permission failure')
    monkeypatch.setattr(Path, 'symlink_to', denied)
    with pytest.raises(PermissionError, match='unrelated'):
        _project_path(tmp_path / 'source', tmp_path / 'destination')
