"""Verify release evidence; local maintainer attestation recovery is permitted only for 0.2.3."""

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import ci_validation
import local_release_evidence as local
import release_recovery as recovery


def gh(*arguments):
    result = subprocess.run(["gh", *arguments], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def legacy_gate():
    commit = os.environ["GITHUB_SHA"]
    repository = os.environ["GITHUB_REPOSITORY"]
    runs = gh("run", "list", "--repo", repository, "--workflow", "bigquery-lifecycle.yml",
              "--commit", commit, "--status", "success", "--limit", "100",
              "--json", "databaseId,headSha")
    required = {"lifecycle (dbt1)", "lifecycle (dbt2)"}
    for run in runs:
        if run["headSha"] != commit:
            continue
        jobs = gh("api", f"repos/{repository}/actions/runs/{run['databaseId']}/jobs?per_page=100")
        passed = {job["name"] for job in jobs["jobs"] if job["conclusion"] == "success"}
        if required <= passed:
            print(f"Credentialed BigQuery gate passed for {commit}, run {run['databaseId']}")
            return
    raise SystemExit("Release blocked: run the credentialed BigQuery lifecycle workflow on this "
                     "exact commit and require both dbt1 and dbt2 jobs to pass. Skipped jobs "
                     "and offline smoke tests do not satisfy this gate.")


def commit_tree(repository, sha):
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Expected an immutable commit SHA")
    value = gh("api", f"repos/{repository}/commits/{sha}")
    if value["sha"] != sha:
        raise ValueError("Unexpected commit response")
    return value["commit"]["tree"]["sha"]


def required_ci_jobs():
    return {f"lint-and-test ({version})" for version in ci_validation.PYTHONS} | {
        "package-smoke (3.11)", "package-smoke (3.14)", "bigquery-compile", "dbt-v2",
        "portability (windows-latest, 3.11)", "portability (macos-latest, 3.14)",
        "postgres-core", "required",
    }


def verify_run(repository, run_id, *, workflow, expected_jobs, tree, now=None):
    if not str(run_id).isdigit():
        raise ValueError("Expected a workflow run ID")
    now = now or datetime.now(UTC)
    prefix = f"repos/{repository}/actions"
    run = gh("api", f"{prefix}/runs/{run_id}")
    reference = gh("api", f"{prefix}/workflows/{workflow}")
    if (run.get("repository", {}).get("full_name") != repository
            or run.get("workflow_id") != reference["id"]
            or run.get("path", "").split("@")[0] != ".github/workflows/" + workflow
            or run.get("status") != "completed" or run.get("conclusion") != "success"
            or run.get("event") not in ({"workflow_dispatch"} if workflow == "full-validation.yml"
                                       else {"pull_request", "push"})
            or commit_tree(repository, run["head_sha"]) != tree):
        raise ValueError("Wrong, incomplete or unsuccessful validation run")
    started = datetime.fromisoformat(run["run_started_at"])
    if not now - timedelta(days=14) <= started <= now:
        raise ValueError("Stale validation run")
    attempt = run["run_attempt"]
    pages = gh("api", "--paginate", "--slurp",
               f"{prefix}/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
    jobs = [job for page in pages for job in page["jobs"]]
    for name in expected_jobs:
        matching = [job for job in jobs if job.get("name") == name]
        if len(matching) != 1 or matching[0].get("conclusion") != "success":
            raise ValueError(f"Missing or unsuccessful required job: {name}")
    return run, pages


def artifact_files(repository, run, name):
    pages = gh("api", "--paginate", "--slurp",
               f"repos/{repository}/actions/runs/{run['id']}/artifacts?per_page=100")
    artifacts = [item for page in pages for item in page["artifacts"] if item["name"] == name]
    if len(artifacts) != 1:
        raise ValueError(f"Missing or duplicate validation artifact: {name}")
    artifact = artifacts[0]
    binding = artifact.get("workflow_run", {})
    if (artifact.get("expired") or binding.get("id") != run["id"]
            or binding.get("head_sha") != run["head_sha"]
            or not 0 < artifact["size_in_bytes"] <= local.MAX_BUNDLE_BYTES):
        raise ValueError("Expired or mismatched validation artifact")
    raw = subprocess.run(["gh", "api",
        f"repos/{repository}/actions/artifacts/{artifact['id']}/zip"], check=True,
        capture_output=True).stdout
    if len(raw) > local.MAX_BUNDLE_BYTES or "sha256:" + local.digest(raw) != artifact.get("digest"):
        raise ValueError("Incorrect validation artifact digest")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        if (sum(item.file_size for item in members) > local.MAX_BUNDLE_BYTES
                or len({item.filename for item in members}) != len(members)):
            raise ValueError("Invalid validation archive")
        return {item.filename: archive.read(item).decode() for item in members
                if not item.is_dir() and Path(item.filename).name in ("evidence.json", "junit.xml")}


def warehouse_report(repository, run, name, tree, *, core=False):
    files = artifact_files(repository, run, name)
    records = [json.loads(value) for path, value in files.items() if Path(path).name == "evidence.json"]
    reports = [value for path, value in files.items() if Path(path).name == "junit.xml"]
    if len(records) != 1 or len(reports) != 1:
        raise ValueError("Missing warehouse record or JUnit report")
    record = records[0]
    if (record.get("schema_version") != 1 or record.get("scope") != "postgres-and-platform"
            or record.get("repository") != repository or record.get("worktree_dirty") is not False
            or record.get("run_id") != str(run["id"])
            or record.get("run_attempt") != str(run["run_attempt"])
            or record.get("tree") != tree or commit_tree(repository, record["sha"]) != tree):
        raise ValueError("Stale or mismatched warehouse checkout evidence")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "junit.xml"
        path.write_text(reports[0])
        counts = ci_validation.junit_counts(path, core=core, minimum=8 if core else 41)
    if counts != record.get("counts"):
        raise ValueError("Warehouse counts disagree with JUnit")
    if not core:
        cases = list(ET.fromstring(reports[0]).iter("testcase"))
        names = [case.attrib["name"] for case in cases]
        if (len(names) != len(set(names)) or set(names) != local.source_names("test_postgres.py")
                or any(case.attrib.get("classname", "").split(".")[-1] != "test_postgres" for case in cases)):
            raise ValueError("Incorrect full Postgres test selection")
    installed, matrix = record.get("installed", {}), record.get("matrix", {})
    if (installed.get("dbt-core") != matrix.get("engine")
            or installed.get("dbt-postgres") != "1.11.0"
            or not installed.get("python", "").startswith(matrix.get("python", "missing") + ".")
            or installed.get("postgres", "").split(".")[0] != matrix.get("postgres")
            or not re.fullmatch(r"[0-9a-f]{64}", record.get("dependency_digest", ""))):
        raise ValueError("Installed warehouse versions disagree with the matrix")
    if core and (record.get("job") != "postgres-core" or record.get("suite") != "postgres-core"
                 or record.get("matrix") != {"python": "3.14", "postgres": "16", "engine": "1.12.6"}):
        raise ValueError("Incorrect Postgres core matrix entry")
    return record


def local_gate(args):
    repository = os.environ["GITHUB_REPOSITORY"]
    if repository != local.REPOSITORY or args.version != recovery.RELEASE_VERSION:
        raise ValueError("The local attestation exception is restricted to dbt-dqm 0.2.3")
    if tomllib.loads((local.ROOT / "pyproject.toml").read_text())["project"]["version"] != args.version:
        raise ValueError("The package version does not match the attestation")
    actual = os.environ["GITHUB_SHA"]
    tree = commit_tree(repository, actual)
    if (local.command("git", "rev-parse", "HEAD") != actual
            or local.command("git", "rev-parse", "HEAD^{tree}") != tree
            or local.command("git", "status", "--porcelain", "--untracked-files=no")
            or commit_tree(repository, recovery.BASE_SHA) != recovery.BASE_TREE):
        raise ValueError("The checkout and tested candidate must have identical trees")
    record = recovery.verify_source()
    if (args.tested_sha != recovery.TESTED_SHA or args.full_run != recovery.FULL_RUN
            or args.digest != recovery.BUNDLE_DIGEST):
        raise ValueError("Recovery must use the approved historical evidence")
    if not args.verify_only:
        if (os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
                or os.environ.get("GITHUB_REF") != "refs/tags/v0.2.3"
                or not args.attest):
            raise ValueError("Publication requires explicit manual attestation on v0.2.3")
        actor = os.environ["GITHUB_ACTOR"]
        permission = gh("api", f"repos/{repository}/collaborators/{actor}/permission")
        if permission.get("permission") != "admin":
            raise ValueError("Only a repository administrator may submit local release attestation")
        main = gh("api", f"repos/{repository}/commits/main")
        if main["commit"]["tree"]["sha"] != tree:
            raise ValueError("Main changed after validation")
    value = local.validate_bundle(args.evidence, args.digest, tree=recovery.BASE_TREE,
        lock_digest=local.digest(recovery.git("show", recovery.BASE_SHA + ":uv.lock")))
    for sha in {run["sha"] for run in value["runs"]}:
        if commit_tree(repository, sha) != recovery.BASE_TREE:
            raise ValueError("A local report names a different source tree")
    ci, _ = verify_run(repository, args.ci_run, workflow="ci.yml",
                       expected_jobs=required_ci_jobs(), tree=tree)
    if not args.verify_only and (ci["event"] != "push" or ci["head_sha"] != actual):
        raise ValueError("Publication requires current CI on the merged/tagged commit")
    warehouse_report(repository, ci, "warehouse-core-acceptance", tree, core=True)
    baseline_ci, _ = verify_run(repository, recovery.BASE_CI, workflow="ci.yml",
        expected_jobs=required_ci_jobs(), tree=recovery.BASE_TREE)
    warehouse_report(repository, baseline_ci, "warehouse-core-acceptance", recovery.BASE_TREE, core=True)
    full, pages = verify_run(repository, args.full_run, workflow="full-validation.yml",
                             expected_jobs=ci_validation.full_jobs() | {"validated"}, tree=recovery.BASE_TREE)
    if full["head_sha"] != args.tested_sha:
        raise ValueError("Full validation must identify the recorded tested SHA")
    records = []
    for name, entry in ci_validation.warehouse_matrix().items():
        artifact = ("warehouse-postgres-lower-bound-acceptance" if name == "dbt-lower-bound" else
                    f"warehouse-postgres-py{entry['python']}-pg{entry['postgres']}-acceptance")
        records.append(warehouse_report(repository, full, artifact, recovery.BASE_TREE))
    ci_validation.verify_jobs(pages, {"verified_api_jobs": {"result": "success"}}, records,
        sha=full["head_sha"], tree=recovery.BASE_TREE, run_id=str(full["id"]), run_attempt=str(full["run_attempt"]),
        repository=repository)
    if args.recovery_record:
        args.recovery_record.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, sort_keys=True))
    print(f"Verified scoped recovery to tree {tree}; local BigQuery results are maintainer-attested.")
    if args.verify_only:
        print("Verification only: no publishing, release mutation or approval requested.")


def main(argv=()):
    if not argv:
        return legacy_gate()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--tested-sha", required=True)
    parser.add_argument("--ci-run", required=True)
    parser.add_argument("--full-run", required=True)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--attest", action="store_true")
    parser.add_argument("--recovery-record", type=Path)
    try:
        local_gate(parser.parse_args(argv))
    except (ValueError, KeyError, OSError, ET.ParseError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        parser.exit(1, f"Release blocked: {error}\n")


if __name__ == "__main__":
    main(sys.argv[1:])
