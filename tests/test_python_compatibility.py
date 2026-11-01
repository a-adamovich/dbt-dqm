"""Verify supported runtimes and compatibility floors in distributable metadata."""

import os
import subprocess
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

ROOT = Path(__file__).resolve().parents[1]
SUPPORTED = ("3.11", "3.12", "3.13", "3.14")


def _assert_python_range(value):
    specifier = SpecifierSet(value)
    assert all(version in specifier for version in SUPPORTED)
    assert "3.10" not in specifier
    assert "3.15" not in specifier


def _assert_compatibility_floors(requirements):
    by_name = {r.name: r.specifier for r in map(Requirement, requirements)}
    for name, old, supported in [("streamlit", "1.51", "1.52")]:
        assert old not in by_name[name]
        assert supported in by_name[name]


def test_project_and_lock_advertise_supported_python_range():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    _assert_python_range(project["requires-python"])
    assert SpecifierSet(project["requires-python"]) == SpecifierSet(lock["requires-python"])
    for version in SUPPORTED:
        assert f"Programming Language :: Python :: {version}" in project["classifiers"]
    _assert_compatibility_floors(project["dependencies"])


def test_dbt_cli_initializes_and_loads_both_adapters():
    from dbt_dqm_app.warehouse import resolve_dbt_executable
    result = subprocess.run(
        [resolve_dbt_executable(), "--version"],
        capture_output=True, text=True, timeout=60, check=False,
        env={**os.environ, "DBT_VERSION_CHECK": "false"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "bigquery:" in result.stdout
    assert "postgres:" in result.stdout


@pytest.mark.skipif(not os.environ.get("DBT_DQM_WHEEL"), reason="Set by CI after building")
def test_wheel_advertises_supported_python_and_compatibility_floors():
    with zipfile.ZipFile(os.environ["DBT_DQM_WHEEL"]) as wheel:
        name = next(n for n in wheel.namelist() if n.endswith(".dist-info/METADATA"))
        metadata = Parser().parsestr(wheel.read(name).decode())
    _assert_python_range(metadata["Requires-Python"])
    _assert_compatibility_floors(metadata.get_all("Requires-Dist"))
