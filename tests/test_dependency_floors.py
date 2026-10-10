"""Security floors for transitive dependencies are part of the distributable metadata (#24).

A wheel doesn't carry uv.lock, so a normal install could otherwise keep a vulnerable version that
still satisfies the upstream ranges. These tests check the project metadata and, when CI sets
DBT_DQM_WHEEL, the built wheel's Requires-Dist. CI also installs the wheel over the old versions.
"""

import os
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path

import pytest
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]

# package: (last vulnerable version, first patched version), from the resolved advisories.
REVIEWED = {
    "sqlparse": ("0.5.5", "0.6.0"),
    "urllib3": ("2.7.0", "2.8.0"),
    "oauthlib": ("3.3.1", "4.0.0"),
    "pyarrow": ("23.0.0", "23.0.1"),
}


def _project_requirements():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    return [Requirement(text) for text in project["dependencies"]]


def _wheel_requirements(path):
    with zipfile.ZipFile(path) as wheel:
        name = next(n for n in wheel.namelist() if n.endswith(".dist-info/METADATA"))
        metadata = Parser().parsestr(wheel.read(name).decode())
    return [Requirement(text) for text in metadata.get_all("Requires-Dist") or []]


def _assert_floors(requirements):
    by_name = {requirement.name: requirement for requirement in requirements}
    for name, (vulnerable, patched) in REVIEWED.items():
        assert name in by_name, f"{name} has no security floor"
        assert vulnerable not in by_name[name].specifier, f"{name} {vulnerable} is allowed"
        assert patched in by_name[name].specifier, f"{name} {patched} is excluded"


def test_project_metadata_excludes_vulnerable_versions():
    _assert_floors(_project_requirements())


@pytest.mark.skipif(not os.environ.get("DBT_DQM_WHEEL"), reason="Set by CI after building")
def test_built_wheel_metadata_excludes_vulnerable_versions():
    _assert_floors(_wheel_requirements(os.environ["DBT_DQM_WHEEL"]))
