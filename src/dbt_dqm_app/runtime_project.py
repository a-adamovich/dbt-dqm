"""An isolated root materialization override, without editing a consumer's project."""

from __future__ import annotations

import re
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

import yaml

_ROOT_TEST_MATERIALIZATION = re.compile(
    r"\{%-?\s*materialization\s+test(?:\s*,[^%]*)?\s*-?%\}.*?"
    r"\{%-?\s*endmaterialization\s*-?%\}", re.DOTALL)


def _hide_root_test_materializations(original, root, paths):
    """Preserve other root macros while selecting DQM's test materialization in this run."""
    result = []
    for path in paths:
        source = (original / path).resolve()
        modified = {}
        if source.is_dir():
            for item in source.rglob("*.sql"):
                text = item.read_text(encoding="utf-8")
                if _ROOT_TEST_MATERIALIZATION.search(text):
                    modified[item.relative_to(source)] = _ROOT_TEST_MATERIALIZATION.sub(
                        lambda match: "{# DQM's dedicated test materialization is selected here. #}"
                        + "\n" * match.group().count("\n"), text)
        if not modified:
            result.append(path)
            continue
        if source == original:
            raise ValueError("A root test materialization in macro-path '.' cannot be isolated; "
                             "place root macros in a dedicated macro directory.")
        clone = Path(tempfile.mkdtemp(prefix="__dbt_dqm_consumer_macros_", dir=root))
        shutil.copytree(source, clone, dirs_exist_ok=True, symlinks=True)
        for relative, text in modified.items():
            destination = clone / relative
            destination.unlink()
            destination.write_text(text, encoding="utf-8")
        result.append(str(clone))
    return result


@contextmanager
def test_project(project_dir, variables, *, compile_only=False, packages_install_path=None):
    """dbt requires builtin materialization overrides to belong to the root project.

    Keep the same project name and relative source paths (hence test identities/FQNs).
    Sources and installed packages are symlinked, while only the project config and one
    explicit root forwarding macro are generated. Outputs always go to the caller's
    absolute target path. The original sources, project config, and parse cache are untouched.
    """
    from .config import _render

    original = Path(project_dir).resolve()
    with tempfile.TemporaryDirectory(prefix="dbt-dqm-test-project-") as directory:
        root = Path(directory)
        project = yaml.safe_load((original / "dbt_project.yml").read_text(encoding="utf-8"))
        manifests = {"packages.yml", "dependencies.yml", "package-lock.yml"}
        for source in original.iterdir():
            if source.name in {"dbt_project.yml", ".git", ".venv", "target", "logs", *manifests}:
                continue
            (root / source.name).symlink_to(source, target_is_directory=source.is_dir())
        # A local package declaration is relative to the real project, not this projection.
        for name in manifests:
            source = original / name
            if not source.is_file():
                continue
            document = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
            for package in document.get("packages", []):
                if isinstance(package, dict) and isinstance(package.get("local"), str):
                    package["local"] = str((original / package["local"]).resolve())
            (root / name).write_text(yaml.safe_dump(document), encoding="utf-8")
        # Paths outside the project must continue resolving against the original root.
        for key in ("model-paths", "seed-paths", "test-paths", "snapshot-paths", "analysis-paths",
                    "macro-paths", "docs-paths", "asset-paths", "function-paths"):
            if key in project:
                paths = _render(project[key], variables)
                project[key] = [str((original / path).resolve())
                                if Path(path).is_absolute() or ".." in Path(path).parts else path
                                for path in paths]
        package_path = packages_install_path or _render(
            project.get("packages-install-path", "dbt_packages"), variables)
        installed = (original / package_path).resolve()
        if compile_only:
            # dbt v2 runs even empty global hooks during compile. A read-only preflight
            # must not execute consumer or package hooks before validation/confirmation.
            project.pop("on-run-start", None)
            project.pop("on-run-end", None)
            packages = Path(tempfile.mkdtemp(prefix="__dbt_dqm_packages_", dir=root))
            if installed.is_dir():
                for source in installed.iterdir():
                    package_config = source / "dbt_project.yml"
                    if not source.is_dir() or not package_config.is_file():
                        (packages / source.name).symlink_to(source, target_is_directory=source.is_dir())
                        continue
                    destination = packages / source.name
                    destination.mkdir()
                    for child in source.iterdir():
                        if child.name != "dbt_project.yml":
                            (destination / child.name).symlink_to(child, target_is_directory=child.is_dir())
                    config = yaml.safe_load(package_config.read_text(encoding="utf-8"))
                    config.pop("on-run-start", None)
                    config.pop("on-run-end", None)
                    (destination / "dbt_project.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
            project["packages-install-path"] = str(packages)
        else:
            if project.get("packages-install-path"):
                project["packages-install-path"] = str(installed)
            paths = _hide_root_test_materializations(original, root, project.get("macro-paths", ["macros"]))
            macros = Path(tempfile.mkdtemp(prefix="__dbt_dqm_runtime_", dir=root))
            (macros / "test.sql").write_text("""{% materialization test, default %}
  {{ return(dbt_dqm.materialization_test_default()) }}
{% endmaterialization %}
""", encoding="utf-8")
            project["macro-paths"] = paths if "." in paths else [*paths, macros.name]
        (root / "dbt_project.yml").write_text(yaml.safe_dump(project), encoding="utf-8")
        yield root
