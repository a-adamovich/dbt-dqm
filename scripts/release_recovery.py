"""Explicit 0.2.2-evidence to 0.2.3 recovery; no general evidence reuse."""

import hashlib
import json
import subprocess
import tomllib
import zipfile
from pathlib import Path

import yaml

BASE_SHA = "67fd1c74436422070273abe40c0b5d3b1b950df9"
TESTED_SHA = "1b164f70ed1ee527b9baaf0553b532474d3cc901"
BASE_TREE = "3e2d4837fa28007301709f41d03d39e8ffd9093d"
FULL_RUN = "42448608634"
BASE_CI = "42448868316"
BUNDLE_DIGEST = "2a675d50b4f6b762e130ae1df1c9b375fa511e2055effb881c4e3bc7df7df974"
RELEASE_VERSION = "0.2.3"
ROOT = Path(__file__).resolve().parents[1]
# Reviewed finite paths, not globs. Runtime, warehouse and matrix files are excluded.
ALLOWED = frozenset({
    ".github/workflows/release.yml", "scripts/check_bigquery_release_gate.py",
    "scripts/download_release_evidence.py", "scripts/release_recovery.py",
    "tests/test_release_workflow.py", "tests/test_local_release_evidence.py",
    "tests/test_release_recovery.py", "AGENTS.md", "README.md", "CHANGELOG.md",
    "docs/release-checklist.md", "docs/dbt-2-compatibility.md",
    "docs/release-notes-0.2.3.md", "docs/operations.md",
})
VERSION_FILES = {"pyproject.toml", "dbt_project.yml", "uv.lock", "src/dbt_dqm_app/__init__.py"}


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True).stdout


def normalized(data, path, version):
    if path.endswith(".py"):
        token = ('__version__ = "' + version + '"').encode()
        if data.count(token) != 1:
            raise ValueError("Unexpected application version metadata")
        return data.replace(token, b'__version__ = "VERSION"')
    value = yaml.safe_load(data) if path.endswith(".yml") else tomllib.loads(data.decode())
    if path == "pyproject.toml":
        if value["project"]["version"] != version:
            raise ValueError("Unexpected Python package version")
        value["project"]["version"] = "VERSION"
    elif path == "dbt_project.yml":
        if value["version"] != version:
            raise ValueError("Unexpected dbt package version")
        value["version"] = "VERSION"
    else:
        roots = [entry for entry in value["package"] if entry["name"] == "dbt-dqm"]
        if len(roots) != 1 or roots[0]["version"] != version:
            raise ValueError("Unexpected lockfile package version")
        roots[0]["version"] = "VERSION"
    return value


def verify_source():
    if git("rev-parse", BASE_SHA + "^{tree}").decode().strip() != BASE_TREE:
        raise ValueError("Unexpected recovery baseline tree")
    changed = set(git("diff", "--no-renames", "--name-only", BASE_SHA, "HEAD").decode().splitlines())
    if not changed <= ALLOWED | VERSION_FILES:
        raise ValueError("Recovery modifies unapproved files: " + ", ".join(sorted(changed - ALLOWED - VERSION_FILES)))
    for path in VERSION_FILES:
        baseline = git("show", BASE_SHA + ":" + path)
        candidate = git("show", "HEAD:" + path)
        if normalized(baseline, path, "0.2.2") != normalized(candidate, path, RELEASE_VERSION):
            raise ValueError("Recovery changes dependencies or package configuration: " + path)
    return {"schema_version": 1, "evidence_version": "0.2.2", "release_version": RELEASE_VERSION,
            "baseline_sha": BASE_SHA, "baseline_tree": BASE_TREE, "tested_sha": TESTED_SHA,
            "release_sha": git("rev-parse", "HEAD").decode().strip(),
            "release_tree": git("rev-parse", "HEAD^{tree}").decode().strip(),
            "changed_files": sorted(changed), "evidence_digest": BUNDLE_DIGEST,
            "full_run": FULL_RUN, "baseline_ci_run": BASE_CI}


def wheel_payload(path, version):
    with zipfile.ZipFile(path) as archive:
        result = {}
        for name in archive.namelist():
            if name.endswith(("/", ".dist-info/RECORD")):
                continue
            key = name.replace("dbt_dqm-" + version + ".dist-info/", "dbt_dqm-VERSION.dist-info/")
            data = archive.read(name)
            if key == "dbt_dqm_app/__init__.py":
                data = normalized(data, "src/dbt_dqm_app/__init__.py", version)
            if key.endswith(".dist-info/METADATA"):
                headers, separator, description = data.partition(b"\n\n")
                headers = headers.replace(("\nVersion: " + version + "\n").encode(), b"\nVersion: VERSION\n")
                # Hatch embeds README in METADATA; only its release-version substitutions
                # may differ. Dependency headers are never subjected to this replacement.
                description = description.replace(version.encode(), b"VERSION")
                data = headers + separator + description
            result[key] = hashlib.sha256(data).hexdigest()
        return result


def verify_wheels(baseline, candidate):
    if wheel_payload(baseline, "0.2.2") != wheel_payload(candidate, RELEASE_VERSION):
        raise ValueError("Installed wheel payload differs beyond version metadata")


if __name__ == "__main__":
    print(json.dumps(verify_source(), indent=2))
