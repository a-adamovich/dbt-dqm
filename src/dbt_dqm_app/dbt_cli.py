"""The dbt process boundary: CLI arguments, structured events and JSON artifacts only."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from contextlib import nullcontext
from functools import cached_property, lru_cache
from pathlib import Path
from types import SimpleNamespace

import yaml
from packaging.version import InvalidVersion, Version

from .errors import AppError

logger = logging.getLogger(__name__)


class Record(dict):
    """Attribute access to serialized dbt records, without importing engine internals."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


def records(value):
    if isinstance(value, dict):
        return Record((key, records(item)) for key, item in value.items())
    if isinstance(value, list):
        return [records(item) for item in value]
    return value


def resolve_dbt_executable(explicit=None) -> str:
    requested = explicit or os.environ.get("DBT_DQM_DBT_EXECUTABLE")
    if requested:
        executable = shutil.which(str(Path(requested).expanduser()))
        if not executable:
            raise AppError("The selected dbt executable could not be found. Check "
                           "--dbt-executable or DBT_DQM_DBT_EXECUTABLE.")
    else:
        directory = Path(sys.executable).parent.resolve()
        executable = shutil.which("dbt", path=str(directory))
        # On Windows, which() can insert cwd ahead of even an explicit search path.
        if sys.platform == "win32" and (
            executable is None or Path(executable).resolve().parent != directory
        ):
            executable = next((found for suffix in (".exe", ".cmd", ".bat", ".com")
                               if (found := shutil.which(str(directory / ("dbt" + suffix))))), None)
        executable = executable or shutil.which("dbt")
        if executable is None:
            raise AppError("dbt could not be found. Install dbt==2.0.9 for BigQuery, or "
                           "dbt-core 1.11–1.12 and its warehouse adapter. Select another "
                           "installation with --dbt-executable.")
    return str(Path(executable).resolve())


@lru_cache(maxsize=8)
def engine_version(executable: str) -> Version:
    process = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False,
        env={**os.environ, "DBT_VERSION_CHECK": "false", "DBT_DISABLE_VERSION_CHECK": "true"},
    )
    output = (process.stdout or "") + "\n" + (process.stderr or "")
    # v1 prints 'Core: ... installed: ...'; v2 prints 'dbt 2.0.9'.
    match = re.search(r"(?:installed:\s*|\bdbt\s+(?:version\s*)?[=:]?\s*v?)(\d+\.\d+\.\d+[^\s]*)",
                      re.sub(r"\x1b\[[0-9;]*m", "", output), re.IGNORECASE)
    try:
        if process.returncode or not match:
            raise ValueError
        version = Version(match[1])
    except (ValueError, InvalidVersion):
        raise AppError("Could not determine the selected dbt version.", output.strip()) from None
    logger.info("dbt executable: %s\ndbt version: %s", executable, version)
    return version


class RunArtifact:
    def __init__(self, raw, manifest):
        self.raw = raw
        self.results = []
        for row in raw.get("results", []):
            item = records(row)
            item["node"] = manifest.nodes.get(row["unique_id"], records({
                "unique_id": row["unique_id"], "name": row["unique_id"], "config": {"meta": {}},
            }))
            self.results.append(item)

    def write(self, path):
        Path(path).write_text(json.dumps(self.raw), encoding="utf-8")


class DbtCLI:
    def __init__(self, *, executable=None, callbacks=(), cwd=None, verbose=False, environment=None):
        self.requested_executable = executable
        self.callbacks = callbacks
        self.cwd = cwd
        self.verbose = verbose
        self.environment = environment or {}
        self.manifest = records({"nodes": {}, "sources": {}})

    @cached_property
    def executable(self):
        return resolve_dbt_executable(self.requested_executable)

    @cached_property
    def version(self):
        return engine_version(self.executable)

    @property
    def is_v2(self):
        return self.version.major == 2

    def validate(self, adapter):
        version = self.version
        if not ((version.major == 1 and version.minor in (11, 12)) or
                (version.major == 2 and version >= Version("2.0.9") and not version.is_prerelease)):
            raise AppError("Supported engines are dbt Core 1.11–1.12 and dbt v2 >=2.0.9,<3.0.0.")
        if self.is_v2 and adapter != "bigquery":
            raise AppError("dbt v2 support is currently limited to BigQuery. Use dbt Core "
                           "1.11–1.12 with dbt-postgres for Postgres.")

    def _event(self, line):
        try:
            value = json.loads(line)
        except (ValueError, TypeError):
            return None
        if not isinstance(value, dict) or "unique_id" in value:
            return None
        info, data = value.get("info", {}), value.get("data", {})
        if not isinstance(info, dict) or not isinstance(data, dict):
            return None
        # Both engines expose info.msg; keep data.msg for existing DQM event consumers.
        message = data.get("msg") or info.get("msg") or value.get("message")
        if message is None:
            return None
        info = {**info, "msg": str(message), "level": info.get("level", "info")}
        event = SimpleNamespace(info=SimpleNamespace(**info),
                                data=SimpleNamespace(**{**data, "msg": str(message)}))
        for callback in self.callbacks:
            callback(event)
        return event

    def invoke(self, arguments, *, vars=None):
        arguments = list(arguments)
        command = arguments[0]
        if vars is not None:
            if "--vars" in arguments:
                index = arguments.index("--vars")
                del arguments[index:index + 2]
            arguments += ["--vars", yaml.safe_dump(vars)]
        variables = (yaml.safe_load(arguments[arguments.index("--vars") + 1]) or {}
                     if "--vars" in arguments else {})
        # Capture debug events for query/job metrics; only the workflow's filtered output
        # reaches the terminal unless --verbose was requested.
        arguments += ["--log-format", "json", "--log-level", "debug", "--write-json"]
        if self.is_v2:
            arguments += ["--no-manage-state"]
        environment = {**os.environ, **self.environment, "DBT_WRITE_JSON": "true", "DBT_QUIET": "false",
                       **({"DBT_ENGINE_BATCH_TESTS": "false"} if self.is_v2 else {})}
        target = Path(arguments[arguments.index("--target-path") + 1]
                      if "--target-path" in arguments else environment.get("DBT_TARGET_PATH", "target"))
        if not target.is_absolute():
            project = Path(arguments[arguments.index("--project-dir") + 1]
                           if "--project-dir" in arguments else self.cwd or Path.cwd())
            target = project / target
        expected = ("manifest.json" if command in ("parse", "compile") else
                    "run_results.json" if command in ("test", "run", "build", "seed") else None)
        if expected:
            (target / expected).unlink(missing_ok=True)
        try:
            executable = [self.executable]
            if not self.is_v2 and command == "compile" and "--no-introspect" in arguments:
                directory = Path(self.executable).parent
                candidates = (directory / "python", directory / "python.exe",
                              directory.parent / "python.exe")
                interpreter = next((path for path in candidates if path.is_file()), None)
                if interpreter:
                    executable = [str(interpreter), str(Path(__file__).with_name("dbt1_entrypoint.py"))]
            projection = nullcontext(None)
            compile_projection = command == "compile" and self.is_v2 and variables.get("_dbt_dqm_workflow")
            if (command == "test" and variables.get("_dbt_dqm_workflow")) or compile_projection:
                from .config import _render, resolve_profiles_dir, resolve_project_dir
                from .runtime_project import test_project

                project = resolve_project_dir(arguments[arguments.index("--project-dir") + 1]
                                              if "--project-dir" in arguments else None)
                if "--project-dir" not in arguments:
                    arguments += ["--project-dir", str(project)]
                if "--profiles-dir" not in arguments:
                    arguments += ["--profiles-dir", str(resolve_profiles_dir())]
                if "--log-path" not in arguments and not environment.get("DBT_LOG_PATH"):
                    configuration = yaml.safe_load((project / "dbt_project.yml").read_text(encoding="utf-8"))
                    log_path = _render(configuration.get("log-path", "logs"), variables)
                    arguments += ["--log-path", str((project / log_path).resolve())]
                projection = test_project(project, variables, compile_only=compile_projection,
                                          packages_install_path=environment.get("DBT_PACKAGES_INSTALL_PATH"))
            with projection as runtime_project:
                if runtime_project is not None:
                    if compile_projection and environment.get("DBT_PACKAGES_INSTALL_PATH"):
                        projected_config = yaml.safe_load((runtime_project / "dbt_project.yml").read_text())
                        environment["DBT_PACKAGES_INSTALL_PATH"] = projected_config["packages-install-path"]
                    index = arguments.index("--project-dir")
                    arguments[index + 1] = str(runtime_project)
                    # Relative state and output paths must not become relative to the projection.
                    for flag in ("--target-path", "--state", "--defer-state", "--log-path"):
                        if flag in arguments:
                            index = arguments.index(flag) + 1
                            arguments[index] = str((project / arguments[index]).resolve())
                    for key in ("DBT_STATE", "DBT_DEFER_STATE", "DBT_LOG_PATH"):
                        if environment.get(key):
                            environment[key] = str((project / environment[key]).resolve())
                process = subprocess.run([*executable, *arguments], capture_output=True, text=True,
                                         encoding="utf-8", errors="replace", check=False,
                                         cwd=self.cwd, env=environment)
            stdout, stderr = process.stdout or "", process.stderr or ""
            errors = []
            for line in (stdout + "\n" + stderr).splitlines():
                event = self._event(line)
                if event and event.info.level == "error":
                    errors.append(event.info.msg)
                if self.verbose:
                    print(line, flush=True)
            result = None
            if command == "ls" and process.returncode == 0:
                result = []
                for line in stdout.splitlines():
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and "unique_id" not in row:
                        message = (row.get("data", {}).get("msg") or row.get("info", {}).get("msg"))
                        try:
                            row = json.loads(message)
                        except (ValueError, TypeError):
                            continue
                    if isinstance(row, dict) and "unique_id" in row:
                        result.append(json.dumps(row))
            elif expected and (target / expected).exists():
                raw = json.loads((target / expected).read_text(encoding="utf-8"))
                if not raw.get("metadata", {}).get("invocation_id"):
                    raise ValueError(f"dbt produced {expected} without an invocation_id")
                if command in ("parse", "compile"):
                    self.manifest = records(raw)
                    result = self.manifest
                else:
                    manifest_path = target / "manifest.json"
                    if not manifest_path.exists():
                        raise ValueError("dbt did not produce manifest.json for this execution")
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if manifest.get("metadata", {}).get("invocation_id") != raw["metadata"]["invocation_id"]:
                        raise ValueError("dbt manifest and run results belong to different invocations")
                    self.manifest = records(manifest)
                    result = RunArtifact(raw, self.manifest)
            elif expected and process.returncode == 0:
                raise ValueError(f"dbt completed without producing {expected}; refusing stale evidence")
            exception = None
            if process.returncode and result is None:
                exception = RuntimeError("\n".join(errors) or (stdout + "\n" + stderr).strip() or
                                         f"dbt exited with status {process.returncode}")
            return SimpleNamespace(success=process.returncode == 0, result=result,
                                   exception=exception, returncode=process.returncode)
        except (OSError, ValueError) as error:
            return SimpleNamespace(success=False, result=None, exception=error, returncode=2)
