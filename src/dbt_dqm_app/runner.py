"""Explicit DQM orchestration. dbt objects stay inside one isolated worker process."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

from .adapters import require_adapter
from .config import load_config


def launch(arguments: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="dbt-dqm-run-") as directory:
        request = Path(directory) / "request.json"
        request.write_text(json.dumps(vars(arguments)), encoding="utf-8")
        return subprocess.run([sys.executable, "-m", "dbt_dqm_app.runner", str(request)],
                              check=False).returncode


def validate_test(node, default_limit: int) -> list[str]:
    """Validate selected nodes before initialization or a destructive full refresh."""
    problems = []
    meta = node.config.meta or {}
    dqm = meta.get("dbt_dqm")
    if not isinstance(dqm, dict):
        return ["meta.dbt_dqm must be an object"]
    if node.config.store_failures is not True:
        problems.append("set store_failures=true")
    grain = dqm.get("granularity")
    if (not isinstance(grain, list) or not grain
            or any(not isinstance(key, str) or not key.strip() for key in grain)):
        problems.append("set meta.dbt_dqm.granularity to a nonempty list of column names")
    elif len({key.strip().lower() for key in grain}) != len(grain):
        problems.append("granularity contains duplicate column names")
    for key in ("poc_responsible", "owner_column", "dqm_granularity"):
        if key in dqm or key in meta:
            problems.append(f"remove obsolete {key}; use granularity and owner metadata/dqm_owner")
    for owner in (meta.get("owner"), dqm.get("owner")):
        if owner is not None and not isinstance(owner, str):
            problems.append("owner must be a string")
    priority = dqm.get("priority")
    if priority is not None and (not isinstance(priority, str) or priority.strip().lower() not in
                                 ("critical", "high", "medium", "low", "unset")):
        problems.append("priority must be critical, high, medium, low or unset")
    if dqm.get("criticality") is not None and not isinstance(dqm["criticality"], str):
        problems.append("criticality must be a string")
    limit = node.config.limit if node.config.limit is not None else default_limit
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        problems.append("native test limit must be a positive integer")
    if re.sub(r"\s+", "", node.config.fail_calc.lower()) != "count(*)":
        problems.append("use fail_calc=count(*)")
    if node.config.store_failures_as not in (None, "table"):
        problems.append("use table failure storage")
    return problems


class Workflow:
    def __init__(self, request, factory=None):
        from dbt.cli.main import dbtRunner

        self.request = request
        variables = yaml.safe_load(request.get("vars") or "{}")
        if not isinstance(variables, dict):
            raise TypeError("--vars must be a YAML object")
        if "dqm_enabled" in variables and variables["dqm_enabled"] is not True:
            raise ValueError("dbt-dqm run requires dqm_enabled=true; omit that override")
        variables["dqm_enabled"] = True
        self.variables = variables
        self.base = ["--vars", yaml.safe_dump(variables)]
        for key in ("project_dir", "profiles_dir", "target"):
            if request.get(key) is not None:
                value = request[key]
                if key.endswith("dir"):
                    value = str(Path(value).expanduser().resolve())
                self.base.extend(["--" + key.replace("_", "-"), value])
        if request.get("threads") is not None and request["threads"] <= 0:
            raise ValueError("--threads must be positive")
        self.settings = {}
        self.columns = []
        self.factory = factory or dbtRunner
        self.runner = self.factory(callbacks=[self.event])
        self.timings = {}
        self.sql_calls = {}
        self.query_jobs = {}
        self.phase = "startup"

    def event(self, event):
        if getattr(event.data, "sql", None):
            self.sql_calls[self.phase] = self.sql_calls.get(self.phase, 0) + 1
        link = re.search(r"j=bq:[^:]+:([^&\s]+)", getattr(event.info, "msg", ""))
        if link:
            self.query_jobs.setdefault(self.phase, []).append(link.group(1))
        message = getattr(event.data, "msg", "")
        if message.startswith("DQM_SETTINGS="):
            self.settings = json.loads(message.removeprefix("DQM_SETTINGS="))
        if message.startswith("DQM_COLUMNS="):
            self.columns = json.loads(message.removeprefix("DQM_COLUMNS="))

    def invoke(self, arguments, **kwargs):
        self.phase = arguments[1].removeprefix("dbt_dqm.") if arguments[0] == "run-operation" else arguments[0]
        started = time.monotonic()
        result = self.runner.invoke([*arguments, *self.base], **kwargs)
        phase = arguments[1].removeprefix("dbt_dqm.") if arguments[0] == "run-operation" else arguments[0]
        self.timings[phase] = self.timings.get(phase, 0) + time.monotonic() - started
        return result

    def checked(self, arguments, **kwargs):
        result = self.invoke(arguments, **kwargs)
        if not result.success:
            raise RuntimeError(f"DQM phase failed: {' '.join(arguments[:2])}. {result.exception or ''}")
        return result.result

    def execute(self) -> int:
        config = load_config(self.request.get("project_dir"), self.request.get("profiles_dir"),
                             self.request.get("target"), cli_vars=self.variables)
        require_adapter(config.adapter_type)
        manifest = self.checked(["parse"])
        self.runner = self.factory(manifest=manifest, callbacks=[self.event])
        self.checked(["run-operation", "dbt_dqm.workflow_settings"])
        default = self.settings["default_limit"]
        listing = ["ls", "--quiet", "--resource-type", "test", "--output", "json",
                   "--output-keys", "unique_id", "fqn"]
        for key in ("select", "exclude"):
            if self.request.get(key):
                listing.extend(["--" + key, *self.request[key]])
        selected = self.checked(listing)
        ids = {json.loads(row)["unique_id"] for row in selected}
        nodes = [node for key, node in manifest.nodes.items()
                 if key in ids and "dbt_dqm" in (node.config.meta or {})]
        errors = []
        for node in nodes:
            label = f"{node.name} ({node.original_file_path})"
            errors.extend(f"{label}: {problem}" for problem in validate_test(node, default))
            if node.config.limit is None:
                print(f"WARNING: {label}: native limit missing; using {default}", flush=True)
                node.config.limit = default
            metadata = node.config.meta.get("dbt_dqm")
            for key in ("capture_mode", "context_columns"):
                if isinstance(metadata, dict) and key in metadata:
                    print(f"WARNING: {label}: {key} is ignored; all output attributes are captured", flush=True)
        if errors:
            raise ValueError("Invalid DQM tests:\n" + "\n".join(errors))
        if self.settings.get("legacy_tag") is not None:
            raise ValueError("Remove obsolete dbt_dqm_test_tag; meta.dbt_dqm identifies DQM tests, and tags are business categories")
        if self.settings.get("legacy_capture") is not None:
            print("WARNING: dbt_dqm_capture_mode is ignored; all output attributes are captured", flush=True)
        if not self.settings["raw_retention"]:
            print("WARNING: full captured values have no dbt_dqm_retention_days configured", flush=True)
        if self.settings["event_payloads"] == "full" and not self.settings["event_retention"]:
            print("WARNING: full event values have no dbt_dqm_event_retention_days configured", flush=True)
        selectors = ["fqn:" + ".".join(node.fqn) for node in nodes]
        if nodes:
            self.checked(["compile", "--no-introspect", "--select", *selectors])
            self.checked(["run-operation", "dbt_dqm.validate_outputs", "--args",
                          json.dumps({"test_ids": [node.unique_id for node in nodes]})])
            for node in nodes:
                node.config.materialized = "dqm_test"
        if self.request.get("full_refresh"):
            from .refresh import full_refresh

            full_refresh(config, manifest, self)
        self.checked(["run-operation", "dbt_dqm.initialize_dqm"])
        tests = None
        if nodes:
            arguments = ["test", "--select", *selectors]
            if self.request.get("threads") is not None:
                arguments += ["--threads", str(self.request["threads"])]
            tests = self.invoke(arguments, vars={**self.variables, "_dbt_dqm_output_columns": {
                row["unique_id"]: row["columns"] for row in self.columns}})
            if tests.result is None:
                raise RuntimeError(f"DQM test execution failed: {tests.exception}")
            results = [r for r in tests.result.results if r.node.unique_id in ids
                       and "dbt_dqm" in (r.node.config.meta or {})]
            # Write directly from this result object while its invocation context is current.
            # This also avoids stale native artifacts when DBT_WRITE_JSON is disabled.
            target = config.target_path / "dqm_run_results.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            tests.result.write(str(target))
            metadata = json.loads(target.read_text(encoding="utf-8"))["metadata"]
            payload = {"invocation_id": metadata["invocation_id"], "results": [
                {"unique_id": r.node.unique_id, "status": str(r.status), "failures": r.failures,
                 "execution_time": r.execution_time, "message": r.message} for r in results]}
            self.checked(["run-operation", "dbt_dqm.collect_results", "--args", json.dumps({"payload": payload})])
        models = ["run", "--select", "package:dbt_dqm"]
        if self.request.get("threads") is not None:
            models += ["--threads", str(self.request["threads"])]
        model_results = self.checked(models)
        from .refresh import restore_view_grants

        restore_view_grants(config)
        model_artifact = config.target_path / "dqm_model_run_results.json"
        model_artifact.parent.mkdir(parents=True, exist_ok=True)
        model_results.write(str(model_artifact))
        reconciliation = json.loads(model_artifact.read_text(encoding="utf-8"))["metadata"]["invocation_id"]
        self.checked(["run-operation", "dbt_dqm.maintain_dqm"],
                     vars={**self.variables, "_dbt_dqm_maintenance_invocation_id": reconciliation})
        (config.target_path / "dqm_workflow_metrics.json").write_text(json.dumps({
            "phase_seconds": self.timings, "sql_calls": self.sql_calls, "query_jobs": self.query_jobs}),
            encoding="utf-8")
        print("DQM phase seconds: " + json.dumps(self.timings), flush=True)
        if tests and (tests.exception or any(str(r.status) in ("error", "skipped")
                                            for r in tests.result.results)):
            return 2
        return 1 if tests and not tests.success else 0


def main() -> int:
    try:
        request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        return Workflow(request).execute()
    except Exception as error:  # noqa: BLE001 - serialize through stderr, never pickle dbt objects.
        print(f"DQM execution error: {error}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
