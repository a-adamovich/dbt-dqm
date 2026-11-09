"""Version-scoped local acceptance receipts. These are maintainer attestations, not CI proof."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path

from query_metrics import job_metrics

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.2.4"
REPOSITORY = "a-adamovich/dbt-dqm"
SUITES = {
    "bigquery-dbt2": ("test_bigquery.py", "2.0.11", 4, 34),
    "bigquery-dbt1": ("test_bigquery.py", "1.12.6", 2, 34),
    "bigquery-permissions": ("test_bigquery_permissions.py", "1.12.6", 0, 1),
}
MAX_BUNDLE_BYTES = 32 * 1024 * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


def command(*args):
    return subprocess.run(args, cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def installed_environment(python):
    return json.loads(command(str(python), '-c',
        'import json,sys,platform,re; from importlib.metadata import distributions; '
        'print(json.dumps({"python":sys.version.split()[0],"platform":sys.platform,'
        '"machine":platform.machine(),"packages":{'
        're.sub(r"[-_.]+","-",d.metadata["Name"]).lower():d.version '
        'for d in distributions() if d.metadata["Name"]}}))'))


def validate_environments(environments, engine):
    for scope, package, version in [('app', 'dbt-dqm', VERSION),
                                    ('engine', 'dbt' if engine == '2.0.11' else 'dbt-core', engine)]:
        item = environments.get(scope, {})
        if (not isinstance(item.get('python'), str) or not item['python'].startswith('3.14.')
                or item.get('packages', {}).get(package) != version):
            raise ValueError('Installed validation environment does not match the release protocol')


def identity():
    if command("git", "status", "--porcelain"):
        raise ValueError("Local release validation requires a clean worktree")
    return {"sha": command("git", "rev-parse", "HEAD"),
            "tree": command("git", "rev-parse", "HEAD^{tree}"),
            "lock_digest": digest((ROOT / "uv.lock").read_bytes())}


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


def expected_names(suite):
    filename, _, _, count = SUITES[suite]
    names = source_names(filename)
    if len(names) != count:
        raise ValueError(f"Unexpected checked-in selection for {suite}: {len(names)} != {count}")
    return names


def report_counts(xml, suite):
    cases = list(ET.fromstring(xml).iter("testcase"))
    names = [case.attrib["name"] for case in cases]
    if len(names) != len(set(names)) or set(names) != expected_names(suite):
        raise ValueError(f"Incorrect or incomplete test selection: {suite}")
    module = SUITES[suite][0].removesuffix(".py")
    if any(case.attrib.get("classname", "").split(".")[-1] != module for case in cases):
        raise ValueError("JUnit reports must come from the expected acceptance module")
    counts = {"collected": len(cases), "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for case in cases:
        outcome = next((key for tag, key in (("failure", "failed"), ("error", "errors"),
                       ("skipped", "skipped")) if case.find(tag) is not None), "passed")
        counts[outcome] += 1
    return counts


def run_suite(suite, executable, directory):
    """Run once, retain failures, and refuse changed source or skipped selections."""
    if ROOT == directory.resolve() or ROOT in directory.resolve().parents:
        raise ValueError("Keep private release evidence outside the repository")
    before = identity()
    if not sys.version.startswith("3.14."):
        raise ValueError("Credentialed release acceptance uses Python 3.14")
    maximum = int(os.environ.get("DBT_DQM_BIGQUERY_MAX_BYTES_BILLED", str(1024 ** 3)))
    if maximum <= 0:
        raise ValueError("The acceptance query byte limit must be positive")
    filename, engine, workers, _ = SUITES[suite]
    executable = executable.resolve()
    output = command(str(executable), "--version")
    if not re.search(r"(?:installed:\s*|\bdbt\s+)" + re.escape(engine) + r"(?:\s|$)", output):
        raise ValueError("The selected engine does not match this validation suite")
    engine_python = executable.parent / ('python.exe' if sys.platform == 'win32' else 'python')
    environments = {'app': installed_environment(sys.executable),
                    'engine': installed_environment(engine_python)}
    validate_environments(environments, engine)
    started = datetime.now(UTC)
    validation_id = uuid.uuid4().hex
    directory = directory / (started.strftime("%Y%m%dT%H%M%S") + "-" + suite + "-" + validation_id[:8])
    directory.mkdir(parents=True, mode=0o700)
    args = [sys.executable, "-m", "pytest", "-q", "-rA", "--durations=25"]
    if workers:
        args += ["-n", str(workers)]
    args += [str(ROOT / "integration_tests/acceptance" / filename),
             "--junitxml=" + str(directory / "junit.xml")]
    environment = {**os.environ, "DBT_DQM_DBT_EXECUTABLE": str(executable),
                   "DBT_DQM_VALIDATION_RUN_ID": validation_id,
                   "PATH": str(executable.parent) + os.pathsep + os.environ["PATH"]}
    with (directory / "pytest.log").open("w") as log:
        try:
            result = subprocess.run(args, cwd=ROOT, env=environment, stdout=log,
                                    stderr=subprocess.STDOUT, check=False)
            code = result.returncode
        except KeyboardInterrupt:
            code = 130
    finished = datetime.now(UTC)
    receipt = {"schema_version": 1, "version": VERSION, "repository": REPOSITORY,
               **before, "id": directory.name, "validation_id": validation_id, "suite": suite, "engine": engine,
               "python": sys.version.split()[0], "workers": workers, "command": args,
               "environments": environments,
               "started_at": started.isoformat(), "finished_at": finished.isoformat(),
               "returncode": code, "status": "failed", "diagnosis": "",
               "redact_literals": [os.environ.get("DBT_DQM_BIGQUERY_TEST_PROJECT", ""), "dbt-dqm"],
               "maximum_bytes_billed": maximum, "query_metrics": {"status": "pending"},
               "dependency_digest": digest(command("uv", "pip", "freeze", "--python", sys.executable).encode()),
               "engine_dependency_digest": digest(command("uv", "pip", "freeze", "--python",
                   str(engine_python)).encode())}
    try:
        if identity() != before:
            raise ValueError("Source changed during acceptance")
        receipt["counts"] = report_counts((directory / "junit.xml").read_text(), suite)
        if code == 0 and receipt["counts"]["passed"] == SUITES[suite][3]:
            receipt["status"] = "passed"
    except (ValueError, OSError, ET.ParseError) as error:
        receipt["validation_error"] = str(error)
    receipt_file = directory / "receipt.json"
    receipt_file.write_text(json.dumps(receipt, indent=2) + "\n")
    # Keep the test result even if measurement is interrupted; never rerun tests for metrics.
    try:
        receipt["query_metrics"] = ({"status": "unavailable", "reason": "Interrupted"} if code == 130 else
            job_metrics(os.environ.get("DBT_DQM_BIGQUERY_TEST_PROJECT"), validation_id,
                        started, finished, permissions=suite == "bigquery-permissions"))
    except KeyboardInterrupt:
        receipt["query_metrics"] = {"status": "unavailable", "reason": "Interrupted"}
    receipt_file.write_text(json.dumps(receipt, indent=2) + "\n")
    print(f"{suite}: {receipt['status']}; private evidence: {directory}")
    return 0 if receipt["status"] == "passed" else 1


class Redactor:
    def __init__(self, literals=()):
        self.literals = sorted({value for value in literals if value}, key=len, reverse=True)
        self.mapping = {}

    def token(self, kind, value):
        key = (kind, value)
        if key not in self.mapping:
            self.mapping[key] = f"REDACTED_{kind}_{len(self.mapping) + 1}"
        return self.mapping[key]

    def __call__(self, value):
        # Strip complete keys, bearer tokens and common credential assignments first.
        value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
                       "REDACTED_PRIVATE_KEY", value, flags=re.DOTALL)
        value = re.sub(r"(?i)(bearer\s+)[a-z0-9._~+/=-]+", r"\1REDACTED_TOKEN", value)
        def hide_secret(match):
            quote = match[2][0] if match[2][0] in ("\"", "'") else ""
            return match[1] + quote + "REDACTED_SECRET" + quote
        value = re.sub(r'''(?i)(["']?(?:private_key|access_token|refresh_token|id_token|subject_token|client_secret|password|api_key|authorization)["']?\s*[:=]\s*)("(?:\\.|[^"\\])*"|'[^']*'|[^\s,}\"'<>]+)''',
                       hide_secret, value)
        for literal in self.literals:
            value = re.sub(re.escape(literal) + r"\.([A-Za-z_][A-Za-z0-9_]*)\.",
                           lambda match, literal=literal: self.token("PROJECT", literal) + "." +
                           self.token("DATASET", match[1]) + ".", value)
            value = value.replace(literal, self.token("PROJECT", literal))
        for kind, pattern in (("EMAIL", r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"),
                              ("DATASET", r"\b(?:dqm_acceptance_[A-Za-z0-9_]+|dbt_dqm_qa|dbt_dqm_demo)\b"),
                              ("PATH", r'''(?:/(?:Users|home|private|tmp|var)/|[A-Za-z]:[\\/])[^\s"'<>]*''')):
            value = re.sub(pattern, lambda match, kind=kind: self.token(kind, match.group()), value)
        return value


def bundle(directory, output):
    paths = sorted(directory.glob("*/receipt.json"))
    if not paths:
        raise ValueError("No local receipts")
    if any(path.is_symlink() or path.parent.resolve().parent != directory.resolve() for path in paths):
        raise ValueError("Evidence inputs must be regular files inside the private directory")
    raw = [json.loads(path.read_text()) for path in paths]
    redactor = Redactor(value for run in raw for value in run.get("redact_literals", []))
    value = {"schema_version": 1, "version": VERSION, "repository": REPOSITORY,
             "created_at": datetime.now(UTC).isoformat(), "runs": [], "files": {}}
    def redact_data(item):
        if isinstance(item, str):
            return redactor(item)
        if isinstance(item, list):
            return [redact_data(child) for child in item]
        return item

    for path, run in zip(paths, raw, strict=True):
        run.pop("redact_literals", None)
        for key in ("command", "validation_error", "diagnosis"):
            if key in run:
                run[key] = redact_data(run[key])
        run["files"] = {}
        for filename in ("junit.xml", "pytest.log"):
            source = path.parent / filename
            if source.is_symlink():
                raise ValueError("Evidence attachments must not be symbolic links")
            if source.is_file():
                name = path.parent.name + "/" + filename
                text = redactor(source.read_text(errors="replace"))
                value["files"][name] = text
                run["files"][filename] = {"name": name, "sha256": digest(text.encode())}
        value["runs"].append(run)
    data = (json.dumps(value, indent=2) + "\n").encode()
    if len(data) > MAX_BUNDLE_BYTES:
        raise ValueError("Evidence bundle is too large; review it before publication")
    output.write_bytes(data)
    print("Public bundle SHA256:", digest(data))


def validate_bundle(path, expected_digest, *, tree, lock_digest, now=None):
    data = path.read_bytes()
    if len(data) > MAX_BUNDLE_BYTES or digest(data) != expected_digest:
        raise ValueError("Incorrect evidence bundle size or digest")
    value = json.loads(data)
    if (value.get("schema_version"), value.get("version"), value.get("repository")) != (1, VERSION, REPOSITORY):
        raise ValueError("Unexpected local evidence identity")
    now = now or datetime.now(UTC)
    latest = {}
    seen = set()
    for run in value["runs"]:
        suite = run["suite"]
        if suite not in SUITES or run["id"] in seen or run.get("synthetic"):
            raise ValueError("Duplicate, unknown or synthetic local evidence")
        seen.add(run["id"])
        if (run.get("tree") != tree or run.get("lock_digest") != lock_digest
                or run.get("version") != VERSION or run.get("repository") != REPOSITORY
                or not re.fullmatch(r"[0-9a-f]{40}", run.get("sha", ""))):
            raise ValueError("Local evidence is from a different candidate")
        start, end = (datetime.fromisoformat(run[key]) for key in ("started_at", "finished_at"))
        if start.tzinfo is None or end.tzinfo is None or not now - timedelta(days=14) <= start <= end <= now:
            raise ValueError("Stale or invalid local evidence timestamps")
        for item in run["files"].values():
            if digest(value["files"][item["name"]].encode()) != item["sha256"]:
                raise ValueError("Changed evidence attachment")
        if run["status"] == "passed":
            validate_environments(run.get('environments', {}), SUITES[suite][1])
            counts = report_counts(value["files"][run["files"]["junit.xml"]["name"]], suite)
            if (run.get("returncode") != 0 or counts != run.get("counts")
                    or counts["passed"] != SUITES[suite][3] or "pytest.log" not in run["files"]
                    or run.get("engine") != SUITES[suite][1] or run.get("workers") != SUITES[suite][2]
                    or not run.get("python", "").startswith("3.14.")
                    or not all(re.fullmatch(r"[0-9a-f]{64}", run.get(key, ""))
                               for key in ("dependency_digest", "engine_dependency_digest"))):
                raise ValueError("Incomplete or unsuccessful local acceptance")
        elif run["status"] != "failed" or not run.get("diagnosis", "").strip():
            raise ValueError("Failed local runs require a recorded diagnosis")
        if suite not in latest or end > latest[suite][0]:
            latest[suite] = (end, run)
    if set(latest) != set(SUITES) or any(run["status"] != "passed" for _, run in latest.values()):
        raise ValueError("Every local acceptance suite must finish successfully")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    run = commands.add_parser("run")
    run.add_argument("--suite", choices=SUITES, required=True)
    run.add_argument("--engine", type=Path, required=True)
    run.add_argument("--directory", type=Path, required=True)
    emit = commands.add_parser("bundle")
    emit.add_argument("--directory", type=Path, required=True)
    emit.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "run":
            return run_suite(args.suite, args.engine, args.directory)
        bundle(args.directory, args.output)
        return 0
    except (ValueError, KeyError, OSError, ET.ParseError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Local evidence rejected: {error}\n")


if __name__ == "__main__":
    sys.exit(main())
