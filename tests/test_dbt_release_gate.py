"""A skipped or stale credentialed run must never authorize a v2 compatibility release."""

import json
import runpy
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_bigquery_release_gate.py"


@pytest.mark.parametrize("revision,conclusions,passes", [
    ("candidate", ("success", "success"), True),
    ("candidate", ("success", "skipped"), False),
    ("candidate", ("success", "failure"), False),
    ("older", ("success", "success"), False),
])
def test_release_gate_requires_both_live_engines_on_exact_revision(monkeypatch, revision, conclusions, passes):
    monkeypatch.setenv("GITHUB_SHA", "candidate")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/package")
    def run(command, **kwargs):
        value = ([{"databaseId": 42, "headSha": revision}] if command[1] == "run" else
                 {"jobs": [{"name": f"lifecycle ({engine})", "conclusion": conclusion}
                           for engine, conclusion in zip(("dbt1", "dbt2"), conclusions, strict=True)]})
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")
    monkeypatch.setattr(subprocess, "run", run)
    main = runpy.run_path(str(SCRIPT))["main"]
    if passes:
        main()
    else:
        with pytest.raises(SystemExit, match="Release blocked"):
            main()
