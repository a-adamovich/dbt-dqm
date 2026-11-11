"""A skipped or stale credentialed run must never authorize a v2 compatibility release."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_bigquery_release_gate.py"


def test_gate_imports_without_third_party_packages():
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c",
         ("import runpy,sys; sys.path.insert(0, sys.argv[1]); "
          "runpy.run_path(sys.argv[2], run_name='gate_import_check')"),
         str(SCRIPT.parent), str(SCRIPT)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("revision", ["candidate", "older"])
def test_gate_requires_exact_revision(monkeypatch, revision):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    import importlib
    gate = importlib.import_module("check_bigquery_release_gate")
    monkeypatch.setattr(gate, 'gh', lambda *args: [{"databaseId": 42, "headSha": revision}])
    called = []
    def verify(*args, **kwargs):
        called.append(kwargs)
        return {"id": 42}, []
    monkeypatch.setattr(gate, 'verify_run', verify)
    if revision == 'candidate':
        gate.successful_run('owner/repo', 'bigquery-lifecycle.yml', 'candidate', {'both'}, 'tree')
        assert called[0]['expected_jobs'] == {'both'}
    else:
        with pytest.raises(ValueError, match='exact-commit'):
            gate.successful_run('owner/repo', 'bigquery-lifecycle.yml', 'candidate', {'both'}, 'tree')
        assert not called


def test_gate_has_no_attestation_mode_and_fails_closed(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    import importlib
    gate = importlib.import_module("check_bigquery_release_gate")
    assert not hasattr(gate, "local_gate") and "local_release_evidence" not in SCRIPT.read_text()
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setattr(gate, "commit_tree", lambda *args: "tree")
    monkeypatch.setattr(gate, "gh", lambda *args: [])
    with pytest.raises(ValueError, match="exact-commit"):
        gate.normal_gate()
