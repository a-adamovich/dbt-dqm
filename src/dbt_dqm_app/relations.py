"""Resolve project naming in an isolated dbt process; keep physical names immutable per client."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import replace
from functools import lru_cache
from pathlib import Path

from .errors import AppError


def bind(config, contract):
    required = {"dqm_install", "dqm_issue_occurrences", "dqm_reconciliation_control"}
    if not required <= contract.keys():
        raise AppError(
            "DQM relation contract is incomplete. Upgrade the dbt package and run again."
        )
    rows = tuple(
        sorted(
            (name, item.get("database"), item["schema"], item["identifier"])
            for name, item in contract.items()
        )
    )
    if len({row[1:] for row in rows}) != len(rows) or any(not row[2] or not row[3] for row in rows):
        raise AppError("DQM naming produced empty or duplicate destinations. Check naming macros.")
    return replace(config, relations=rows)


def namespace(config):
    return hashlib.sha256(
        json.dumps(next(row[1:] for row in config.relations if row[0] == "dqm_install")).encode()
    ).hexdigest()[:16]


def resolve_config(config):
    from .adapters import require_adapter

    require_adapter(config.adapter_type)
    destination = (config.target_path or config.project_dir / "target").resolve()
    for key in ("DBT_STATE", "DBT_DEFER_STATE"):
        if (
            os.environ.get(key)
            and (config.project_dir / Path(os.environ[key]).expanduser()).resolve() == destination
        ):
            raise AppError(
                f"{key} directory equals target-path. Keep comparison artifacts separate from output."
            )
    if config.relations:
        return config
    request = json.dumps(
        {
            "project_dir": str(config.project_dir),
            "profiles_dir": str(config.profiles_dir),
            "target": config.target,
            "target_path": str(config.target_path or config.project_dir / "target"),
            "vars": config.vars_yaml,
            "adapter_type": config.adapter_type,
        },
        sort_keys=True,
    )
    # Invalidate cached naming when environment, project, profiles or naming macros change.
    paths = [config.project_dir / "dbt_project.yml", config.profiles_dir / "profiles.yml"]
    paths.extend((config.project_dir / "macros").rglob("*.sql"))
    stamp = tuple(
        (str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in sorted(paths) if p.exists()
    )
    environment = hashlib.sha256(json.dumps(sorted(os.environ.items())).encode()).hexdigest()
    result = _resolve(request, stamp, environment)
    resolved = bind(config, result["relations"])
    return replace(
        resolved,
        test_granularity=tuple(
            sorted((key, tuple(value)) for key, value in result["granularity"].items())
        ),
    )


@lru_cache(maxsize=8)
def _resolve(request, stamp, environment):
    with tempfile.TemporaryDirectory(prefix="dbt-dqm-naming-") as directory:
        source = Path(directory) / "request.json"
        output = Path(directory) / "relations.json"
        source.write_text(request, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "dbt_dqm_app.relations", str(source), str(output)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode or not output.exists():
            raise AppError(
                "Could not resolve DQM relation names. Install the matching dbt package "
                "and check target, --vars and naming macros.",
                result.stderr.strip() or result.stdout.strip(),
            )
        return json.loads(output.read_text(encoding="utf-8"))


def lock_key(config):
    from .warehouse import _relation_parts

    _, schema, identifier = _relation_parts(config, "dqm_install")
    return "dbt_dqm:" + schema + ("." + identifier if identifier != "dqm_install" else "")


def main():
    import yaml
    from dbt.cli.main import dbtRunner

    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    variables = yaml.safe_load(request["vars"]) or {}
    variables["dqm_enabled"] = True
    flags = [
        "--project-dir",
        request["project_dir"],
        "--profiles-dir",
        request["profiles_dir"],
        "--target",
        request["target"],
        "--target-path",
        request["target_path"],
        "--vars",
        yaml.safe_dump(variables),
        "--no-defer",
        "--no-favor-state",
        "--log-level",
        "none",
    ]
    if request.get("adapter_type") == "bigquery":
        from dbt.adapters.bigquery.connections import BigQueryConnectionManager
        from dbt.adapters.contracts.connection import ConnectionState

        original_close = BigQueryConnectionManager.close.__func__

        def close_unused(cls, connection):
            # dbt-bigquery 1.12 closes INIT handles by dereferencing the lazy client, which
            # opens it and loads ADC. Naming compilation never needs that unused client.
            if connection.state != ConnectionState.OPEN:
                connection.handle = None
                connection.state = ConnectionState.CLOSED
                return connection
            return original_close(cls, connection)

        BigQueryConnectionManager.close = classmethod(close_unused)
    contract = {}
    errors = []

    def event(value):
        message = getattr(value.data, "msg", "")
        if message.startswith("DQM_RELATIONS="):
            contract.update(json.loads(message.removeprefix("DQM_RELATIONS=")))
        if str(value.info.level).lower() == "error":
            errors.append(value.info.msg)

    with (
        tempfile.TemporaryFile(mode="w+", encoding="utf-8") as captured,
        contextlib.redirect_stdout(captured),
        contextlib.redirect_stderr(captured),
    ):
        runner = dbtRunner(callbacks=[event])
        parsed = runner.invoke(["parse", *flags])
        result = (
            dbtRunner(manifest=parsed.result, callbacks=[event]).invoke(
                [
                    "--no-populate-cache",
                    "compile",
                    "--no-introspect",
                    "--inline",
                    "{{ dbt_dqm.resolved_relations() }}",
                    *flags,
                ]
            )
            if parsed.success
            else parsed
        )
    if not result.success or not contract:
        print(
            str(result.exception or "\n".join(errors) or "Missing DQM relation contract"),
            file=sys.stderr,
        )
        return 2
    granularity = {}
    for key, node in parsed.result.nodes.items():
        metadata = (node.config.meta or {}).get("dbt_dqm")
        if node.resource_type == "test" and isinstance(metadata, dict):
            values = metadata.get("granularity", [])
            if isinstance(values, list):
                granularity[key] = [value for value in values if isinstance(value, str)]
    Path(sys.argv[2]).write_text(
        json.dumps({"relations": contract, "granularity": granularity}), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
