"""Acceptance harness phases. Product dbt commands have no automatic capture/maintenance hooks.

Protocol tests deliberately keep capture, freeze, apply and maintenance separately invocable.
"""
from __future__ import annotations

import json
import subprocess


def collect(command, result, target, *, maintenance=True):
    artifact = target / "run_results.json"
    if not artifact.exists():
        return result
    original = artifact.read_text(encoding="utf-8")
    data = json.loads(original)
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    tracked = [row for row in data["results"] if row["unique_id"] in manifest["nodes"]
               and "dbt_dqm" in (manifest["nodes"][row["unique_id"]]["config"].get("meta") or {})]
    base = [command[0], "run-operation"]
    # The harness's callers use test/build/run as their first command argument.
    flags = command[2:]
    # Strip selectors and command-specific options while retaining config/vars/artifact paths.
    accepted = ("--project-dir", "--profiles-dir", "--target-path", "--log-path", "--vars", "--target")
    options = []
    for index, flag in enumerate(flags):
        if flag in accepted:
            options.extend((flag, flags[index + 1]))

    def invoke(macro, args=None):
        invocation = [*base, macro, *options]
        if macro == "dbt_dqm.maintain_dqm":
            import yaml

            variables = yaml.safe_load(options[options.index("--vars") + 1]) if "--vars" in options else {}
            if "--vars" in invocation:
                index = invocation.index("--vars")
                del invocation[index:index + 2]
            invocation += ["--vars", json.dumps({**variables, "dqm_enabled": True,
                          "_dbt_dqm_maintenance_invocation_id": data["metadata"]["invocation_id"]})]
        if args is not None:
            invocation += ["--args", json.dumps(args)]
        phase = subprocess.run(invocation, capture_output=True, text=True, timeout=600, check=False,
                               cwd=options[options.index("--project-dir") + 1])
        result.stdout += phase.stdout
        result.stderr += phase.stderr
        if phase.returncode:
            result.returncode = 2
        return phase.returncode == 0

    if tracked:
        payload = {"invocation_id": data["metadata"]["invocation_id"], "results": [
            {key: row.get(key) for key in ("unique_id", "status", "failures", "execution_time", "message")}
            for row in tracked]}
        # initialize_dqm is idempotent and keeps faults local to each protocol test.
        if invoke("dbt_dqm.initialize_dqm"):
            invoke("dbt_dqm.collect_results", {"payload": payload})
    reconciled = any(row["unique_id"] == "model.dbt_dqm.dqm_reconcile" and row["status"] == "success"
                     for row in data["results"])
    if maintenance and (tracked or reconciled) and "--empty" not in command:
        invoke("dbt_dqm.maintain_dqm")
    # Preserve the original test invocation artifact for exact-linkage assertions.
    artifact.write_text(original, encoding="utf-8")
    return result
