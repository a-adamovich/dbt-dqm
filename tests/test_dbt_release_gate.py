"""A skipped or stale credentialed run must never authorize a v2 compatibility release."""

import json
import runpy
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


@pytest.mark.parametrize("revision,conclusions,passes", [
    ("candidate", ("success", "success"), True),
    ("candidate", ("success", "skipped"), False),
    ("candidate", ("success", "failure"), False),
    ("older", ("success", "success"), False),
])
def test_release_gate_requires_both_live_engines_on_exact_revision(monkeypatch, revision, conclusions, passes):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    monkeypatch.setenv("GITHUB_SHA", "candidate")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/package")
    def run(command, **kwargs):
        if command[1] == "run":
            assert command[command.index("--workflow") + 1] == "bigquery-lifecycle.yml"
        value = ([{"databaseId": 42, "headSha": revision}] if command[1] == "run" else
                 {"jobs": [{"name": f"lifecycle ({engine})", "conclusion": conclusion}
                           for engine, conclusion in zip(("dbt1", "dbt2"), conclusions, strict=True)]})
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")
    monkeypatch.setattr(subprocess, "run", run)
    main = runpy.run_path(str(SCRIPT))["legacy_gate"]
    if passes:
        main()
    else:
        with pytest.raises(SystemExit, match="Release blocked"):
            main()
