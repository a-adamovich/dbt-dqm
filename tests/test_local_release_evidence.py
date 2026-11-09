"""Synthetic fixtures exercise gates only; they never authorize a real publication."""

import copy
import importlib
import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TREE = "a" * 40
SHA = "b" * 40
LOCK = "c" * 64
NOW = datetime(2026, 11, 7, 20, tzinfo=UTC)


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return (importlib.import_module("local_release_evidence"),
            importlib.import_module("release_evidence"))


def junit(local, suite, outcome=None):
    root = ET.Element("testsuite")
    for name in sorted(local.expected_names(suite)):
        case = ET.SubElement(root, "testcase", name=name,
                             classname="integration_tests.acceptance." + local.SUITES[suite][0][:-3])
        if outcome:
            ET.SubElement(case, outcome)
    return ET.tostring(root, encoding="unicode")


def evidence(local):
    value = {"schema_version": 1, "version": "0.2.4", "repository": local.REPOSITORY,
             "runs": [], "files": {}}
    for suite, (_, engine, workers, _) in local.SUITES.items():
        xml = junit(local, suite)
        files = {}
        for name, text in (("junit.xml", xml), ("pytest.log", "Synthetic unit-test fixture")):
            path = suite + "/" + name
            value["files"][path] = text
            files[name] = {"name": path, "sha256": local.digest(text.encode())}
        value["runs"].append({"id": suite, "schema_version": 1, "suite": suite,
            "version": "0.2.4", "repository": local.REPOSITORY, "sha": SHA, "tree": TREE,
            "lock_digest": LOCK, "status": "passed", "returncode": 0, "engine": engine,
            "python": "3.14.6", "workers": workers, "dependency_digest": LOCK,
            "engine_dependency_digest": LOCK, "counts": local.report_counts(xml, suite),
            "started_at": (NOW - timedelta(hours=2)).isoformat(),
            "finished_at": (NOW - timedelta(hours=1)).isoformat(), "files": files})
    return value


def validate(local, path, value):
    path.write_text(json.dumps(value))
    return local.validate_bundle(path, local.digest(path.read_bytes()), tree=TREE,
                                 lock_digest=LOCK, now=NOW)


def test_complete_local_selection_passes(modules, tmp_path):
    local, _ = modules
    assert len(local.expected_names("bigquery-dbt2")) == 33
    assert validate(local, tmp_path / "bundle.json", evidence(local))


@pytest.mark.parametrize("change", ["tree", "sha", "engine", "workers", "python", "lock_digest",
    "dependency_digest", "synthetic", "skip", "low_count", "module", "duplicate", "failed",
    "missing_suite", "stale", "future", "attachment", "unknown_suite"])
def test_local_evidence_fails_closed(modules, tmp_path, change):
    local, _ = modules
    value = evidence(local)
    run = value["runs"][0]
    if change in ("tree", "sha", "engine", "python", "lock_digest", "dependency_digest"):
        run[change] = "wrong"
    elif change == "workers":
        run[change] = 6
    elif change == "synthetic":
        run[change] = True
    elif change in ("skip", "low_count", "module", "duplicate"):
        root = ET.fromstring(value["files"][run["files"]["junit.xml"]["name"]])
        if change == "skip":
            ET.SubElement(root[0], "skipped")
        elif change == "low_count":
            root.remove(root[0])
        elif change == "module":
            root[0].set("classname", "unit_tests")
        else:
            root.append(copy.deepcopy(root[0]))
        text = ET.tostring(root, encoding="unicode")
        value["files"][run["files"]["junit.xml"]["name"]] = text
        run["files"]["junit.xml"]["sha256"] = local.digest(text.encode())
    elif change == "failed":
        run["status"] = "failed"
    elif change == "missing_suite":
        value["runs"].pop()
    elif change == "stale":
        run["started_at"] = (NOW - timedelta(days=15)).isoformat()
    elif change == "future":
        run["finished_at"] = (NOW + timedelta(days=1)).isoformat()
    elif change == "attachment":
        value["files"][run["files"]["pytest.log"]["name"]] += "changed"
    else:
        run["suite"] = "unrelated"
    with pytest.raises(ValueError):
        validate(local, tmp_path / "bundle.json", value)


def test_failed_attempts_are_retained_and_require_diagnosis(modules, tmp_path):
    local, _ = modules
    value = evidence(local)
    earlier = copy.deepcopy(value["runs"][0])
    earlier.update(id="failed-earlier", status="failed", returncode=1,
                   finished_at=(NOW - timedelta(minutes=90)).isoformat())
    value["runs"].insert(0, earlier)
    with pytest.raises(ValueError, match="diagnosis"):
        validate(local, tmp_path / "bundle.json", value)
    earlier["diagnosis"] = "Synthetic interrupted-run fixture"
    assert len(validate(local, tmp_path / "bundle.json", value)["runs"]) == 4


def test_redaction_preserves_machine_readable_junit_and_diagnostics(modules):
    local, _ = modules
    redact = local.Redactor(["private-project"])
    original = '''<testsuite><testcase name="test_generic_and_singular_lifecycle" file="/Users/person/work/test.py"><system-out>
private-project.dqm_acceptance_abc123.table
owner@private-project.iam.gserviceaccount.com bearer private-token
{"password":"private-password", "access_token":"private-access"}
-----BEGIN PRIVATE KEY-----
private-key-material
-----END PRIVATE KEY-----
SHA aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa dbt 2.0.11
</system-out></testcase></testsuite>'''
    text = redact(original)
    ET.fromstring(text)
    for secret in ("/Users/", "private-project", "dqm_acceptance_abc123", "owner@",
                   "private-token", "private-password", "private-access", "private-key-material"):
        assert secret not in text
    assert "test_generic_and_singular_lifecycle" in text and TREE in text and "2.0.11" in text
    assert redact("private-project") == redact("private-project")


@pytest.mark.parametrize("message", ["password='private-password'", "access_token=private-token"])
def test_redaction_preserves_xml_attribute_quotes(modules, message):
    local, _ = modules
    root = ET.Element("testsuite")
    ET.SubElement(root, "testcase", name="test_example", message=message)
    redacted = local.Redactor()(ET.tostring(root, encoding="unicode"))
    ET.fromstring(redacted)
    assert "private-password" not in redacted and "private-token" not in redacted


@pytest.mark.parametrize("change", [None, "failed", "skipped", "cancelled", "missing", "tree", "stale", "workflow"])
def test_github_run_requires_all_jobs_and_actual_tree(modules, monkeypatch, change):
    local, gate = modules
    jobs = [{"name": name, "conclusion": "success"} for name in gate.ci_validation.full_jobs() | {"validated"}]
    run = {"id": 7, "run_attempt": 2, "repository": {"full_name": local.REPOSITORY},
           "head_sha": SHA, "workflow_id": 9, "path": ".github/workflows/full-validation.yml",
           "status": "completed", "conclusion": "success", "event": "workflow_dispatch",
           "run_started_at": (NOW - timedelta(hours=3)).isoformat()}
    if change in ("failed", "skipped", "cancelled"):
        next(job for job in jobs if job["name"] == "candidate-qa / memory-and-exports")["conclusion"] = change
    elif change == "missing":
        jobs.pop()
    elif change == "stale":
        run["run_started_at"] = (NOW - timedelta(days=15)).isoformat()
    elif change == "workflow":
        run["workflow_id"] = 10
    def api(*args):
        if args[-1].endswith("/runs/7"):
            return run
        if "/workflows/" in args[-1]:
            return {"id": 9}
        return [{"jobs": jobs}]
    monkeypatch.setattr(gate, "gh", api)
    monkeypatch.setattr(gate, "commit_tree", lambda *a: "d" * 40 if change == "tree" else TREE)
    def check():
        return gate.verify_run(local.REPOSITORY, "7", workflow="full-validation.yml",
            expected_jobs=gate.ci_validation.full_jobs() | {"validated"}, tree=TREE, now=NOW)
    if change is None:
        assert check()[0]["run_attempt"] == 2
    else:
        with pytest.raises(ValueError):
            check()


def test_bundle_digest_is_required(modules, tmp_path):
    local, _ = modules
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(evidence(local)))
    with pytest.raises(ValueError, match="digest"):
        local.validate_bundle(path, "incorrect", tree=TREE, lock_digest=LOCK, now=NOW)


@pytest.fixture
def complete_gate(modules, monkeypatch, tmp_path):
    local, _ = modules
    gate = importlib.import_module("check_bigquery_release_gate")
    monkeypatch.setattr(gate.tomllib, "loads", lambda _: {"project": {"version": "0.2.4"}})
    actual = "d" * 40  # A squash merge changes the SHA, while preserving the complete tree.
    monkeypatch.setenv("GITHUB_REPOSITORY", local.REPOSITORY)
    monkeypatch.setenv("GITHUB_SHA", actual)
    monkeypatch.setenv("GITHUB_ACTOR", "maintainer")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_REF", "refs/tags/v0.2.4")
    monkeypatch.setattr(local, "command", lambda *args: "" if "status" in args else
                        TREE if args[-1] == "HEAD^{tree}" else actual)
    monkeypatch.setattr(local, "validate_bundle", lambda *a, **k: evidence(local))
    looked_up = []
    def tree(repository, sha):
        looked_up.append(sha)
        return TREE
    monkeypatch.setattr(gate, "commit_tree", tree)
    calls = []
    def api(*args):
        calls.append(args)
        if args[-1].endswith("/permission"):
            return {"permission": "admin"}
        return {"commit": {"tree": {"sha": TREE}}}
    monkeypatch.setattr(gate, "gh", api)
    ci = {"id": 6, "run_attempt": 1, "head_sha": actual, "event": "push"}
    full = {"id": 7, "run_attempt": 2, "head_sha": SHA}
    jobs = [{"name": name, "conclusion": "success"} for name in gate.ci_validation.full_jobs() | {"validated"}]
    def verify_run(repository, run_id, *, workflow, expected_jobs, tree):
        if workflow == "full-validation.yml":
            assert "validated" in expected_jobs and len(expected_jobs) == 28
            return full, [{"jobs": jobs}]
        assert "required" in expected_jobs and len(expected_jobs) == 12
        return ci, []
    monkeypatch.setattr(gate, "verify_run", verify_run)
    records = {name: {"schema_version": 1, "scope": "postgres-and-platform", "job": name,
        "repository": local.REPOSITORY, "run_id": "7", "run_attempt": "2", "sha": SHA,
        "tree": TREE, "worktree_dirty": False, "suite": "postgres-full",
        "matrix": {key: entry[key] for key in ("python", "postgres", "engine")},
        "counts": {"collected": 41, "passed": 41, "failed": 0, "errors": 0, "skipped": 0}}
        for name, entry in gate.ci_validation.warehouse_matrix().items()}
    def warehouse(repository, run, name, tree, *, core=False):
        if core:
            return {}
        if "lower-bound" in name:
            return records["dbt-lower-bound"]
        entry = next(key for key, value in gate.ci_validation.warehouse_matrix().items()
                     if f"py{value['python']}-pg{value['postgres']}" in name)
        return records[entry]
    monkeypatch.setattr(gate, "warehouse_report", warehouse)
    argv = ["--version", "0.2.4", "--evidence", str(tmp_path / "synthetic-fixture.json"),
            "--digest", LOCK, "--tested-sha", SHA, "--ci-run", "6", "--full-run", "7"]
    return gate, argv, ci, full, records, looked_up, calls


def test_same_tree_different_sha_passes_verification_only_without_approval(complete_gate, capsys):
    gate, argv, _, _, _, looked_up, calls = complete_gate
    gate.main([*argv, "--verify-only"])
    assert SHA in looked_up and "d" * 40 in looked_up
    assert not calls  # No environment/deployment approval API, or release writes.
    assert "Verification only" in capsys.readouterr().out


@pytest.mark.parametrize("change", [None, "version", "attestation", "ref", "actor", "main_tree",
                                    "current_ci", "full_sha", "report_tree", "attempt"])
def test_publication_verification_enforces_temporary_scope(complete_gate, monkeypatch, capsys, change):
    gate, argv, ci, full, records, _, _ = complete_gate
    if change != "attestation":
        argv.append("--attest")
    if change == "version":
        argv[1] = "0.2.5"
    elif change == "ref":
        monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    elif change in ("actor", "main_tree"):
        monkeypatch.setattr(gate, "gh", lambda *a: {"permission": "write"} if change == "actor" else
                            {"permission": "admin", "commit": {"tree": {"sha": "wrong"}}})
    elif change == "current_ci":
        ci["head_sha"] = SHA
    elif change == "full_sha":
        full["head_sha"] = "e" * 40
    elif change == "report_tree":
        for record in records.values():
            record["tree"] = "e" * 40
    elif change == "attempt":
        next(iter(records.values()))["run_attempt"] = "1"
    if change is None:
        gate.main(argv)
    else:
        with pytest.raises(SystemExit) as error:
            gate.main(argv)
        assert error.value.code == 1
        assert "Release blocked" in capsys.readouterr().err


