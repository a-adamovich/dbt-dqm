"""Exercise the actual release verification script without creating attestations."""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def verify(tmp_path):
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    steps = workflow["jobs"]["release"]["steps"]
    step = next(s for s in steps if s.get("name") == "Verify distribution provenance")
    names = [s.get("name") for s in steps]
    assert names.index("Attest distribution provenance") < names.index(step["name"])
    assert names.index(step["name"]) < names.index("Publish to PyPI through trusted publishing")
    assert step["shell"] == "bash"
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    script = tmp_path / "verify.sh"
    script.write_text(step["run"])
    dist = tmp_path / "dist"
    dist.mkdir()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    gh = binaries / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "log = Path(os.environ['VERIFY_LOG'])\n"
        "calls = json.loads(log.read_text()) if log.exists() else []\n"
        "calls.append(sys.argv[1:])\n"
        "log.write_text(json.dumps(calls))\n"
        "sys.exit(1 if len(calls) <= int(os.environ.get('VERIFY_FAILURES', '0')) else 0)\n"
    )
    gh.chmod(0o755)
    sleep = binaries / "sleep"
    sleep.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$SLEEP_LOG"\n')
    sleep.chmod(0o755)
    log = tmp_path / "calls.json"
    sleep_log = tmp_path / "sleeps.txt"

    def run(files, failures=0):
        for name in files:
            (dist / name).write_text("artifact")
        env = {
            **os.environ,
            "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
            "GITHUB_REPOSITORY": "a-adamovich/dbt-dqm",
            "GITHUB_SHA": "a" * 40,
            "GITHUB_REF": "refs/tags/v0.2.0a1",
            "GH_TOKEN": "test-token",
            "VERIFY_LOG": str(log),
            "SLEEP_LOG": str(sleep_log),
            "VERIFY_FAILURES": str(failures),
        }
        result = subprocess.run(
            ["bash", str(script)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        calls = json.loads(log.read_text()) if log.exists() else []
        sleeps = sleep_log.read_text().splitlines() if sleep_log.exists() else []
        return result, calls, sleeps

    return run


@pytest.mark.parametrize(
    "files",
    [
        [],
        ["package.whl"],
        ["package.tar.gz"],
        ["package.whl", "other.whl", "package.tar.gz"],
        ["package.whl", "package.tar.gz", "unexpected.txt"],
        ["package.whl", "package.tar.gz", ".hidden"],
    ],
)
def test_release_rejects_incomplete_or_extra_distributions(verify, files):
    result, calls, sleeps = verify(files)
    assert result.returncode != 0
    assert "Expected exactly one wheel" in result.stderr
    assert not calls and not sleeps


def test_release_verifies_each_artifact_with_exact_identity(verify):
    result, calls, sleeps = verify(["package.whl", "package.tar.gz"])
    assert result.returncode == 0, result.stderr
    assert not sleeps
    assert calls == [
        [
            "attestation",
            "verify",
            "dist/" + name,
            "--repo",
            "a-adamovich/dbt-dqm",
            "--signer-workflow",
            "a-adamovich/dbt-dqm/.github/workflows/release.yml",
            "--source-digest",
            "a" * 40,
            "--source-ref",
            "refs/tags/v0.2.0a1",
            "--deny-self-hosted-runners",
        ]
        for name in ("package.whl", "package.tar.gz")
    ]


def test_release_retries_then_propagates_verification_failure(verify):
    result, calls, sleeps = verify(["package.whl", "package.tar.gz"], failures=3)
    assert result.returncode != 0
    assert "failed after three attempts" in result.stderr
    assert len(calls) == 3
    assert all(call[2] == "dist/package.whl" for call in calls)
    assert sleeps == ["5", "5"]


def test_release_recovers_after_attestation_visibility_delay(verify):
    result, calls, sleeps = verify(["package.whl", "package.tar.gz"], failures=2)
    assert result.returncode == 0, result.stderr
    assert len(calls) == 4
    assert sleeps == ["5", "5"]


def _tag_check(tmp_path, tag, python_version="0.2.0a1", dbt_version="0.2.0-a1"):
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    steps = workflow["jobs"]["release"]["steps"]
    names = [s.get("name") for s in steps]
    step = steps[names.index("Check the tag matches the package versions")]
    # It runs before anything is installed, built or published.
    assert names.index(step["name"]) < names.index("Verify, build, and inspect distributions")
    assert step["shell"] == "bash"
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "dbt-dqm"\nversion = "{python_version}"\n')
    (tmp_path / "dbt_project.yml").write_text(f"name: 'dbt_dqm'\nversion: {dbt_version}\n")
    script = tmp_path / "check.sh"
    script.write_text(step["run"])
    return subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_REF_NAME": tag},
        capture_output=True,
        text=True,
        check=False,
    )


def test_release_tag_must_match_both_package_versions(tmp_path):
    assert _tag_check(tmp_path, "v0.2.0a1").returncode == 0
    mismatch = _tag_check(tmp_path, "v0.2.1")
    assert mismatch.returncode != 0 and "doesn't match package version v0.2.0a1" in mismatch.stderr
    differ = _tag_check(tmp_path, "v0.2.0a1", dbt_version="0.2.1")
    assert differ.returncode != 0 and "versions differ" in differ.stderr


def test_dbt_spells_a_python_pre_release_with_one_exact_semver_form(tmp_path):
    # dbt's project schema rejects 0.2.0a1, so only the hyphenated SemVer spelling matches.
    for python_version, dbt_version in [("0.2.0a1", "0.2.0-a1"), ("0.2.0a2", "0.2.0-a2"), ("0.2.0rc2", "0.2.0-rc2"), ("0.2.0", "0.2.0")]:
        assert _tag_check(tmp_path, f"v{python_version}", python_version, dbt_version).returncode == 0
    for dbt_version in ("0.2.0a1", "0.2.0-alpha.1", "0.2.0"):
        result = _tag_check(tmp_path, "v0.2.0a1", dbt_version=dbt_version)
        assert result.returncode != 0 and "expected dbt version 0.2.0-a1" in result.stderr
    stable = _tag_check(tmp_path, "v0.2.0", "0.2.0", "0.2.0-a1")
    assert stable.returncode != 0 and "versions differ" in stable.stderr


def test_repository_versions_agree_and_release_installs_locked():
    import tomllib

    python_version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    dbt_version = str(yaml.safe_load((ROOT / "dbt_project.yml").read_text())["version"])
    from dbt_dqm_app import __version__

    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked_version = next(p["version"] for p in lock["package"] if p["name"] == "dbt-dqm")
    assert python_version == __version__ == locked_version
    # dbt requires SemVer, so the release check expects 0.2.0a1 to be spelled 0.2.0-a1.
    assert dbt_version == re.sub(r"^(\d+\.\d+\.\d+)((?:a|b|rc)\d+)$", r"\1-\2", python_version)
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    assert "uv sync --locked --all-groups" in workflow
    # uv otherwise adds dist/.gitignore, which the strict provenance inventory rejects.
    assert "uv build --no-create-gitignore" in workflow


def _create_release(tmp_path, tag):
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    job = workflow["jobs"]["release"]
    assert job["environment"] == "pypi"
    names = [s.get("name") for s in job["steps"]]
    assert names.index("Verify distribution provenance") < names.index(
        "Publish to PyPI through trusted publishing"
    ) < names.index("Create GitHub release")
    step = next(s for s in job["steps"] if s.get("name") == "Create GitHub release")
    binaries = tmp_path / "bin"
    binaries.mkdir(exist_ok=True)
    log = tmp_path / "release.json"
    gh = binaries / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['RELEASE_LOG']).write_text(json.dumps(sys.argv[1:]))\n"
    )
    gh.chmod(0o755)
    script = tmp_path / "release.sh"
    script.write_text(step["run"])
    result = subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
            "GITHUB_REF_NAME": tag,
            "RELEASE_LOG": str(log),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(log.read_text())


def test_only_pre_release_tags_create_a_github_pre_release(tmp_path):
    alpha = _create_release(tmp_path, "v0.2.0a1")
    assert "--prerelease" in alpha
    assert alpha[alpha.index("--title") + 1] == "dbt-dqm v0.2.0a1 (pre-release)"
    notes = alpha[alpha.index("--notes") + 1]
    assert "pip install dbt-dqm==0.2.0a1" in notes and "Git revision v0.2.0a1" in notes
    assert "spells the version 0.2.0-a1" in notes
    stable = _create_release(tmp_path, "v0.2.0")
    assert "--prerelease" not in stable and "--notes" not in stable and "--title" not in stable


def test_alpha_version_is_accepted_by_python_and_dbt():
    from dbt.contracts.project import Project
    from dbt_common.semver import VersionSpecifier
    from packaging.version import Version

    assert Version("0.2.0a1").is_prerelease
    assert Version("0.2.0-a1") == Version("0.2.0a1")
    assert VersionSpecifier.from_version_string("0.2.0-a1").prerelease == "a1"
    # Parsing a version alone is insufficient: dbt validates package project schemas too.
    Project.validate(yaml.safe_load((ROOT / "dbt_project.yml").read_text()))
