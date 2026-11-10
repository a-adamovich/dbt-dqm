"""Shared standard-library checks for release validation evidence."""

import ast
import hashlib
import io
import json
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import ci_validation

ROOT = Path(__file__).resolve().parents[1]
MAX_BUNDLE_BYTES = 32 * 1024 * 1024


def gh(*arguments):
    result = subprocess.run(["gh", *arguments], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def command(*args):
    return subprocess.run(args, cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


# Exact BigQuery smoke selection: Core runs only these; dbt 2 runs the whole module.
BIGQUERY_SMOKE_TESTS = frozenset({
    "test_default_maintenance_shortcuts_keep_retention_fence_and_health",
    "test_bigquery_batch_rollback_lost_response_and_empty",
    "test_bigquery_apply_fencing_overlap_stale_generation_and_abandoned_runner",
    "test_bigquery_dedicated_limits_full_refresh_and_complex_payloads",
    "test_run_changes_sorted_results_and_collection_limits",
    "test_generic_and_singular_lifecycle",
    "test_bigquery_identity_signature_migration_preserves_continuity",
    "test_bigquery_collated_dataset_lifecycle_and_case_sensitive_changes[und:ci]",
    "test_bigquery_collated_tracking_refuses_writes_and_explicit_refresh_recovers[und:ci]",
})


CROSS_ENGINE_CORE_VERSION = "1.12.6"


def source_names(filename):
    """Derive checked-in cases without importing warehouse clients or executing tests."""
    source = ast.parse((ROOT / "integration_tests/acceptance" / filename).read_text())
    names = set()
    for node in source.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        suffixes = [""]
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and ast.unparse(decorator.func) == "pytest.mark.parametrize":
                values = ast.literal_eval(decorator.args[1])
                suffixes = [f"[{value}]" for value in values]
        names.update(node.name + suffix for suffix in suffixes)
    return names


def bigquery_selection(engine):
    """Return the evidence suite and exact case names required for one engine."""
    names = source_names("test_bigquery.py")
    if engine == "dbt2":
        return "bigquery-full", names
    if engine == "dbt1":
        if not BIGQUERY_SMOKE_TESTS <= names:
            raise ValueError("BigQuery smoke selection names a case that does not exist")
        return "bigquery-smoke", set(BIGQUERY_SMOKE_TESTS)
    raise ValueError("Unknown BigQuery engine")


def bigquery_suite(kind, engine):
    """Return the evidence suite, test file and exact case names for one gate job."""
    if kind == "acceptance":
        suite, names = bigquery_selection(engine)
        return suite, "test_bigquery.py", names
    if kind == "permissions":
        if engine != "dbt1":
            raise ValueError("Restricted permissions requires the Core engine")
        return "bigquery-permissions", "test_bigquery_permissions.py", source_names("test_bigquery_permissions.py")
    if kind == "upgrade":
        if engine != "dbt2":
            raise ValueError("The cross-engine upgrade is recorded on dbt 2")
        return "bigquery-upgrade", "test_identity_upgrade.py", source_names("test_identity_upgrade.py")
    raise ValueError("Unknown BigQuery evidence kind")


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
            or run.get("event") not in ({"workflow_dispatch"} if workflow in {"full-validation.yml", "bigquery-lifecycle.yml"}
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
            or not 0 < artifact["size_in_bytes"] <= MAX_BUNDLE_BYTES):
        raise ValueError("Expired or mismatched validation artifact")
    raw = subprocess.run(["gh", "api",
        f"repos/{repository}/actions/artifacts/{artifact['id']}/zip"], check=True,
        capture_output=True).stdout
    if len(raw) > MAX_BUNDLE_BYTES or "sha256:" + digest(raw) != artifact.get("digest"):
        raise ValueError("Incorrect validation artifact digest")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        if (sum(item.file_size for item in members) > MAX_BUNDLE_BYTES
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
            or record.get("sha") != run["head_sha"]
            or record.get("tree") != tree or commit_tree(repository, record["sha"]) != tree):
        raise ValueError("Stale or mismatched warehouse checkout evidence")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "junit.xml"
        path.write_text(reports[0])
        counts = ci_validation.junit_counts(path, core=core, minimum=8 if core else 42)
    if counts != record.get("counts"):
        raise ValueError("Warehouse counts disagree with JUnit")
    if not core:
        cases = list(ET.fromstring(reports[0]).iter("testcase"))
        names = [case.attrib["name"] for case in cases]
        if (len(names) != len(set(names)) or set(names) != source_names("test_postgres.py")
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




def bigquery_report(repository, run, name, tree, engine, *, permissions=False, upgrade=False):
    """Require fresh, same-attempt evidence and the exact BigQuery selection for the engine."""
    files = artifact_files(repository, run, name)
    records = [json.loads(value) for path, value in files.items() if Path(path).name == "evidence.json"]
    reports = [value for path, value in files.items() if Path(path).name == "junit.xml"]
    if len(records) != 1 or len(reports) != 1 or engine not in {"dbt1", "dbt2"}:
        raise ValueError("Missing BigQuery evidence or JUnit report")
    record = records[0]
    version = {"dbt1": "1.12.6", "dbt2": "2.0.11"}[engine]
    kind = 'permissions' if permissions else 'upgrade' if upgrade else 'acceptance'
    suite, filename, expected = bigquery_suite(kind, engine)
    count = len(expected)
    if (upgrade and record.get("installed", {}).get("core") != CROSS_ENGINE_CORE_VERSION) or (
            record.get("schema_version") != 1 or record.get("scope") != "bigquery"
            or record.get("suite") != suite or record.get("repository") != repository
            or record.get("run_id") != str(run["id"])
            or record.get("run_attempt") != str(run["run_attempt"])
            or record.get("sha") != run["head_sha"] or record.get("tree") != tree
            or record.get("worktree_dirty") is not False
            or record.get("matrix") != {"engine": engine, "version": version, "python": '3.14'}
            or record.get("installed", {}).get("dbt") != version
            or not record.get('installed', {}).get('python', '').startswith('3.14.')
            or not re.fullmatch(r"[0-9a-f]{64}", record.get("dependency_digest", ""))
            or commit_tree(repository, record["sha"]) != tree):
        raise ValueError("Mismatched BigQuery evidence")
    cases = list(ET.fromstring(reports[0]).iter("testcase"))
    names = [case.attrib["name"] for case in cases]
    if (len(names) != count or len(set(names)) != count or set(names) != expected
            or any(case.attrib.get("classname", "").split(".")[-1] != filename[:-3] for case in cases)
            or any(any(case.find(tag) is not None for tag in ("failure", "error", "skipped"))
                   for case in cases)
            or record.get("counts") != {"collected": count, "passed": count, "failed": 0,
                                        "errors": 0, "skipped": 0}):
        raise ValueError("Incomplete BigQuery test selection")
    return record
