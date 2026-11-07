"""Explicit DQM orchestration through dbt's CLI and serialized artifacts."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

from .adapters import require_adapter
from .config import load_config
from .dbt_cli import DbtCLI


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
    for key, owner in (("meta.owner", meta.get("owner")),
                       ("meta.dbt_dqm.owner", dqm.get("owner"))):
        if not (owner is None or isinstance(owner, str) or
                isinstance(owner, list) and all(isinstance(value, str) for value in owner)):
            problems.append(f"{key} must be a string, list of strings or null; "
                            f"effective value {owner!r} ({type(owner).__name__}); "
                            "metadata may be inherited from dbt_project.yml")
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


def test_result_key(result):
    status = str(result.status).lower()
    priority = {"error": 0, "fail": 1, "warn": 2, "skipped": 3, "pass": 4}
    return (priority.get(status, 5), status, result.node.name.casefold(), result.node.unique_id)


class Workflow:
    def __init__(self, request, factory=None):
        self.request = dict(request)
        variables = yaml.safe_load(request.get("vars") or "{}")
        if not isinstance(variables, dict):
            raise TypeError("--vars must be a YAML object")
        if "dqm_enabled" in variables and variables["dqm_enabled"] is not True:
            raise ValueError("dbt-dqm run requires dqm_enabled=true; omit that override")
        variables["dqm_enabled"] = True
        variables["_dbt_dqm_workflow"] = True
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
        self.collection_errors = []
        self.collection_summary = {}
        self.maintenance = []
        self.console = sys.stderr
        self.phase_errors = []
        self.phase_messages = []
        self.outcomes = {}
        self.selected_count = 0
        self.cancelled = False
        self.reconciliation_id = None
        self.owner_summary = None
        self.summary_status = "not_requested"
        self.engine_info = {}
        self.runner = (factory(callbacks=[self.event]) if factory else DbtCLI(
            executable=request.get("dbt_executable"), callbacks=[self.event],
            verbose=bool(request.get("verbose"))))
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
        if message.startswith("DQM_COLLECTION_ERRORS="):
            self.collection_errors = json.loads(message.removeprefix("DQM_COLLECTION_ERRORS="))
        if message.startswith("DQM_COLUMNS="):
            self.columns = json.loads(message.removeprefix("DQM_COLUMNS="))
        if message.startswith("DQM_COLLECTION_SUMMARY="):
            self.collection_summary = json.loads(message.removeprefix("DQM_COLLECTION_SUMMARY="))
        if message.startswith("DQM_MAINTENANCE="):
            self.maintenance = json.loads(message.removeprefix("DQM_MAINTENANCE="))
        if message.startswith("DQM_MAINTENANCE_STEP="):
            self.maintenance.append(json.loads(message.removeprefix("DQM_MAINTENANCE_STEP=")))
        level = str(getattr(event.info, "level", "")).lower()
        if level in ("warn", "warning", "error") and not message.startswith("DQM_"):
            reason = getattr(event.info, "msg", "") or message
            if reason and reason not in self.phase_messages:
                self.phase_messages.append(reason)
                if level == "error":
                    self.phase_errors.append(reason)
                duplicate_test = self.phase == 'test' and getattr(event.info, 'name', '') in {
                    'LogTestResult', 'RunResultWarning', 'RunResultWarningMessage',
                    'RunResultFailure', 'RunResultError', 'RunResultErrorNoMessage'}
                if not self.request.get("verbose") and not duplicate_test:
                    print(reason, file=self.console, flush=True)

    def phase_flags(self, command):
        flags = []
        if command in ("ls", "compile", "test") and self.request.get("state") is not None:
            flags += ["--state", self.request["state"]]
        if command in ("compile", "test"):
            for key in ("defer", "favor_state"):
                if self.request.get(key) is not None:
                    flags.append("--" + ("" if self.request[key] else "no-") +
                                 key.replace("_", "-"))
        else:
            flags += ["--no-defer", "--no-favor-state"]
        if command in ("test", "run") and not getattr(self.runner, "is_v2", False):
            flags.append("--cache-selected-only")
        if isinstance(self.runner, DbtCLI):
            return flags
        if not self.request.get("verbose"):
            flags += ["--log-level", "none"]
        else:
            flags += ["--no-quiet", "--log-level", "info"]
        return flags

    def invoke(self, arguments, **kwargs):
        self.phase = arguments[1].removeprefix("dbt_dqm.") if arguments[0] == "run-operation" else arguments[0]
        started = time.monotonic()
        self.phase_errors = []
        self.phase_messages = []
        print(f"DQM: {self.phase.replace('_', ' ')}…", flush=True)
        try:
            with contextlib.ExitStack() as stack:
                if not self.request.get("verbose"):
                    output = stack.enter_context(tempfile.TemporaryFile(mode="w+", encoding="utf-8"))
                    stack.enter_context(contextlib.redirect_stdout(output))
                    stack.enter_context(contextlib.redirect_stderr(output))
                return self.runner.invoke([*arguments, *self.base,
                                           *self.phase_flags(arguments[0])], **kwargs)
        finally:
            self.timings[self.phase] = self.timings.get(self.phase, 0) + time.monotonic() - started

    def failure_reason(self, result):
        if result.exception:
            return str(result.exception)
        failures = [r.message for r in getattr(result.result, "results", [])
                    if str(r.status) in ("error", "fail", "skipped") and r.message]
        return "\n".join(failures or self.phase_errors) or (
            "dbt returned an unsuccessful result; run again with --verbose for details")

    def checked(self, arguments, **kwargs):
        result = self.invoke(arguments, **kwargs)
        if not result.success:
            raise RuntimeError(f"DQM phase failed: {' '.join(arguments[:2])}. {self.failure_reason(result)}")
        return result.result

    def report_test_results(self, results):
        for result in sorted(results, key=test_result_key):
            status = str(result.status)
            self.outcomes[status] = self.outcomes.get(status, 0) + 1
            if not self.request.get('verbose'):
                count = result.failures if result.failures is not None else 'unavailable'
                reason = ('; ' + ' '.join((result.message or '').split())
                          if status in ('error', 'skipped', 'fail') else '')
                print(f'DQM test {status}: {result.node.name}: {count} rows{reason}', flush=True)

    def execute(self) -> int:
        started = time.monotonic()
        config = None
        code = 2
        try:
            config = load_config(self.request.get("project_dir"), self.request.get("profiles_dir"),
                                 self.request.get("target"), cli_vars=self.variables,
                                 target_path=self.request.get("target_path"),
                                 dbt_executable=self.request.get("dbt_executable"))
            self.base += ["--target-path", str(config.target_path)]
            for key, value in (("state", self.request.get("state", None)
                               if self.request.get("state") is not None else os.environ.get("DBT_STATE")),
                               ("defer_state", os.environ.get("DBT_DEFER_STATE"))):
                if value or (key == "state" and self.request.get("state") is not None):
                    path = (config.project_dir / Path(value).expanduser()).resolve()
                    if path == config.target_path:
                        config = None  # Never write metrics into the input state directory.
                        raise ValueError(f"{key.replace('_', '-')} directory equals target-path: "
                                         f"{path}. Keep input state separate from output artifacts.")
                    if key == "state" and self.request.get("state") is not None:
                        self.request["state"] = str(path)
            code = self._execute(config)
            return code
        finally:
            elapsed = time.monotonic() - started
            if config is not None:
                metrics = {**self.engine_info, "phase_seconds": self.timings, "sql_calls": self.sql_calls,
                           "query_jobs": self.query_jobs, "total_seconds": elapsed,
                           "completion_status": "cancelled" if self.cancelled else
                           {0: "success", 1: "test_failures", 2: "error"}[code],
                           "exit_code": code, "maintenance_steps": self.maintenance,
                           "test_outcomes": self.outcomes, "collection": self.collection_summary,
                           "reconciliation_id": self.reconciliation_id,
                           "owner_summary": self.owner_summary, "summary_status": self.summary_status}
                try:
                    config.target_path.mkdir(parents=True, exist_ok=True)
                    (config.target_path / "dqm_workflow_metrics.json").write_text(
                        json.dumps(metrics), encoding="utf-8")
                except OSError as error:
                    print(f"WARNING: Could not write DQM metrics: {error}", file=sys.stderr)
            captured = self.collection_summary.get("captured_issue_count")
            capture_notice = (f"{captured} captured issues" if captured is not None else
                              "capture count unavailable" if self.selected_count else "0 captured issues")
            outcomes = ", ".join(f"{count} {status}" for status, count in sorted(self.outcomes.items()))
            limited = self.collection_summary.get("limit_reached_count")
            limit_notice = (f"{limited} collections reached the limit" if limited is not None else
                            "collection limit count unavailable" if self.selected_count else
                            "0 collections reached the limit")
            if self.cancelled:
                print("DQM cancelled: no tracking relations reset; no tests run.", flush=True)
            else:
                print(f"DQM {'complete' if code != 2 else 'stopped'}: {self.selected_count} tests"
                      f"{f' ({outcomes})' if outcomes else ''}, {capture_notice}, {limit_notice}, "
                      f"{elapsed:.1f}s.",
                      flush=True)
                if limited:
                    tests = sorted(self.collection_summary.get("limit_reached_tests", []),
                                   key=lambda row: (row["test_name"].casefold(), row["test_unique_id"]))
                    names = ", ".join(row["test_name"] for row in tests)
                    print(f"WARNING: Collections reached the limit: {names}. Total failures are "
                          "unknown; missing issues are not archived from limited evidence.", flush=True)
            if self.owner_summary is not None:
                from .summary import print_summary
                print_summary(self.owner_summary)
            if self.request.get("verbose"):
                print("DQM phase seconds: " + json.dumps(self.timings), flush=True)

    def _execute(self, config) -> int:
        require_adapter(config.adapter_type)
        if isinstance(self.runner, DbtCLI):
            self.runner.validate(config.adapter_type)
            self.engine_info = {"dbt_version": str(self.runner.version),
                                "dbt_executable": self.runner.executable}
        manifest = self.checked(["parse"])
        self.checked(["run-operation", "dbt_dqm.workflow_settings"])
        if self.settings.get('relations'):
            from .relations import bind
            config = bind(config, self.settings['relations'])
        default = self.settings["default_limit"]
        listing = ["ls", "--resource-type", "test", "--output", "json",
                   "--output-keys", "unique_id", "fqn"]
        for key in ("select", "exclude"):
            if self.request.get(key):
                listing.extend(["--" + key, *self.request[key]])
        selected = self.checked(listing)
        ids = {json.loads(row)["unique_id"] for row in selected}
        nodes = [node for key, node in manifest.nodes.items()
                 if key in ids and "dbt_dqm" in (node.config.meta or {})]
        self.selected_count = len(nodes)
        if not nodes:
            self.collection_summary = {"captured_issue_count": 0, "limit_reached_count": 0,
                                       "limit_reached_tests": []}
        errors = []
        for other in manifest.nodes.values():
            if other.resource_type != "test" or other.unique_id in ids or "dbt_dqm" not in (other.config.meta or {}):
                continue
            for problem in validate_test(other, default):
                if "store_failures" in problem or "granularity" in problem:
                    print(f"WARNING: {other.name} ({other.original_file_path}): {problem} (not selected)", flush=True)
        for node in nodes:
            label = f"{node.name} ({node.original_file_path})"
            errors.extend(f"{label}: {problem}" for problem in validate_test(node, default))
            if node.config.limit is None:
                print(f"WARNING: {label}: native limit missing; using {default}", flush=True)
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
            if isinstance(self.runner, DbtCLI):
                from .runtime_project import test_project

                # Validate that the isolated root override can be constructed before a
                # full refresh or any tracking-table initialization is allowed to run.
                with test_project(config.project_dir, self.variables,
                                  packages_install_path=os.environ.get("DBT_PACKAGES_INSTALL_PATH")):
                    pass
            compiled = self.checked(["compile", "--no-introspect", "--select", *selectors])
            compiled_nodes = getattr(compiled, "nodes", manifest.nodes)
            queries = {node.unique_id: getattr(compiled_nodes[node.unique_id], "compiled_code", "")
                       for node in nodes}
            if isinstance(self.runner, DbtCLI) and any(not query for query in queries.values()):
                raise RuntimeError("dbt did not produce compiled SQL for every selected DQM test")
            self.checked(["run-operation", "dbt_dqm.validate_outputs", "--args",
                          json.dumps({"test_ids": [node.unique_id for node in nodes],
                                      "compiled_queries": queries})])
        if self.request.get("full_refresh"):
            from .refresh import confirm_refresh, full_refresh

            if not confirm_refresh(config, manifest, self):
                self.cancelled = True
                return 0

            self.phase = "full_refresh"
            print("DQM: full refresh…", flush=True)
            started = time.monotonic()
            try:
                full_refresh(config, manifest, self)
            finally:
                self.timings["full_refresh"] = time.monotonic() - started
        self.checked(["run-operation", "dbt_dqm.initialize_dqm"])
        tests = None
        capture_failed = False
        if nodes:
            arguments = ["test", "--select", *selectors]
            if self.request.get("threads") is not None:
                arguments += ["--threads", str(self.request["threads"])]
            tests = self.invoke(arguments, vars={**self.variables, "_dbt_dqm_output_columns": {
                row["unique_id"]: row["columns"] for row in self.columns}})
            if tests.result is None:
                raise RuntimeError(f"DQM test execution failed: {self.failure_reason(tests)}")
            results = [r for r in tests.result.results if r.node.unique_id in ids
                       and "dbt_dqm" in (r.node.config.meta or {})]
            missing = {node.unique_id for node in nodes} - {r.node.unique_id for r in results}
            if missing:
                capture_failed = True
                print("DQM tests produced no result: " + ", ".join(sorted(missing)),
                      file=sys.stderr, flush=True)
            self.report_test_results(results)
            # Write directly from this result object while its invocation context is current.
            # This also avoids stale native artifacts when DBT_WRITE_JSON is disabled.
            target = config.target_path / "dqm_run_results.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            tests.result.write(str(target))
            metadata = json.loads(target.read_text(encoding="utf-8"))["metadata"]
            payload = {"invocation_id": metadata["invocation_id"], "results": [
                {"unique_id": r.node.unique_id, "status": str(r.status), "failures": r.failures,
                 "execution_time": r.execution_time, "message": r.message} for r in results]}
            try:
                self.checked(["run-operation", "dbt_dqm.collect_results", "--args", json.dumps({"payload": payload})])
            except RuntimeError as error:
                # Earlier tests can already have committed evidence. Reconcile those commits;
                # pending/error executions remain unusable and never imply resolution.
                capture_failed = True
                print(f"DQM collection error: {error}", file=sys.stderr, flush=True)
            if self.collection_errors:
                capture_failed = True
                for error in self.collection_errors:
                    print(f"DQM collection error: {error['test_name']}: {error['status']}: {error['message']}",
                          file=sys.stderr, flush=True)
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
        self.reconciliation_id = reconciliation
        if self.request.get('summary'):
            from .summary import owner_summary
            started = time.monotonic()
            try:
                self.owner_summary = owner_summary(config, reconciliation)
                self.summary_status = 'available'
            except Exception as error:  # noqa: BLE001 - reporting must not change execution outcomes.
                self.summary_status = 'unavailable'
                print(f'WARNING: Owner summary unavailable: {error}', file=sys.stderr, flush=True)
            finally:
                self.timings['summary'] = time.monotonic() - started
        self.checked(["run-operation", "dbt_dqm.maintain_dqm"],
                     vars={**self.variables, "_dbt_dqm_maintenance_invocation_id": reconciliation})
        for step in self.maintenance:
            if step.get("ok") is False:
                print(f"WARNING: DQM maintenance {step['step']} failed; see maintenance Health.", flush=True)
        if capture_failed or tests and (tests.exception or any(str(r.status) in ("error", "skipped")
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
