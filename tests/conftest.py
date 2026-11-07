"""Shared fixtures for testing the serialized dbt process boundary."""

import json
import subprocess
from pathlib import Path

import pytest
from packaging.version import Version


@pytest.fixture
def fake_dbt_process(monkeypatch):
    monkeypatch.setattr("dbt_dqm_app.dbt_cli.engine_version", lambda *args: Version("1.12.5"))

    def run(command, **kwargs):
        if command[1] == "parse":
            target = Path(command[command.index("--target-path") + 1])
            target.mkdir(parents=True, exist_ok=True)
            (target / "manifest.json").write_text(json.dumps({
                "metadata": {"invocation_id": "test"}, "nodes": {}, "sources": {},
            }))
        return subprocess.CompletedProcess(command, 0, "", "")

    return run
