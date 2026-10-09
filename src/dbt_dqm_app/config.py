from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dbt.clients.jinja import get_rendered
from dbt.context.base import generate_base_context
from dbt_common.context import set_invocation_context
from platformdirs import user_data_path

from .limits import MAX_CACHE_ISSUES, MAX_CACHE_MIB, validate_limits


@dataclass(frozen=True)
class AppConfig:
    project_dir: Path
    profiles_dir: Path
    target: str
    profile_name: str
    adapter_type: str
    project_id: str
    dataset: str
    location: str
    method: str
    keyfile: Path | None
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None
    dbname: str | None = None
    sslmode: str | None = None
    archive_cache_days: int = 90
    target_path: Path | None = None
    # How long a Postgres statement waits for a lock (for example, while a reconciliation holds
    # the occurrence table) before the app reports the warehouse as busy.
    lock_timeout_seconds: int = 5
    # Explicit per-client ADC file; useful for testing independent reviewer identities without
    # changing the process environment shared by other clients.
    credentials_file: Path | None = None
    max_cache_issues: int = MAX_CACHE_ISSUES
    max_cache_mib: int = MAX_CACHE_MIB

    def __post_init__(self) -> None:
        validate_limits(self.max_cache_issues, self.max_cache_mib)

    @property
    def max_cache_bytes(self) -> int:
        return self.max_cache_mib * 2**20

    @property
    def workspace_path(self) -> Path:
        return workspace_path_for(self.project_dir, self.profile_name, self.target)


def workspace_path_for(project_dir: Path, profile_name: str, target: str) -> Path:
    identity = f"{project_dir.resolve()}|{profile_name}|{target}"
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return user_data_path("dbt-dqm") / suffix / "workspace.sqlite"


def resolve_workspace_path(project_dir: str | Path, target: str) -> Path:
    project_dir = Path(project_dir).expanduser().resolve()
    project = yaml.safe_load((project_dir / "dbt_project.yml").read_text())
    return workspace_path_for(project_dir, project["profile"], target)


def _plain(value: object) -> str:
    set_invocation_context(os.environ)
    rendered = get_rendered(str(value), generate_base_context({}), native=True)
    return os.path.expandvars(os.path.expanduser(str(rendered)))


def load_config(project_dir: str | Path, profiles_dir: str | Path, target: str) -> AppConfig:
    project_dir = Path(project_dir).expanduser().resolve()
    profiles_dir = Path(profiles_dir).expanduser().resolve()
    project = yaml.safe_load((project_dir / "dbt_project.yml").read_text())
    profile_name = project["profile"]
    profiles = yaml.safe_load((profiles_dir / "profiles.yml").read_text())
    output = profiles[profile_name]["outputs"][target]
    adapter_type = _plain(output.get("type"))
    if adapter_type not in {"bigquery", "postgres"}:
        raise ValueError("The local app supports BigQuery and Postgres profiles")
    method = output.get("method", "oauth")
    keyfile = (
        Path(_plain(output["keyfile"]))
        if adapter_type == "bigquery" and method == "service-account"
        else None
    )
    variables = project.get("vars", {}) or {}
    dqm_vars = variables.get("dbt_dqm", {}) if isinstance(variables.get("dbt_dqm"), dict) else {}
    custom_dataset = dqm_vars.get("schema") or variables.get("dbt_dqm_schema")
    base_schema = output.get("dataset") if adapter_type == "bigquery" else output.get("schema")
    dataset = f"{base_schema}_{custom_dataset}" if custom_dataset else base_schema
    project_id = output.get("project") if adapter_type == "bigquery" else output.get("dbname")
    archive_cache_days = int(os.environ.get("DBT_DQM_ARCHIVE_CACHE_DAYS", "90"))
    if archive_cache_days < 0:
        raise ValueError("DBT_DQM_ARCHIVE_CACHE_DAYS must be zero or a positive integer")
    # dbt-postgres accepts both spellings of the password key.
    password = output.get("pass", output.get("password"))
    lock_timeout_seconds = int(os.environ.get("DBT_DQM_LOCK_TIMEOUT_SECONDS", "5"))
    if lock_timeout_seconds <= 0:
        raise ValueError("DBT_DQM_LOCK_TIMEOUT_SECONDS must be a positive integer")
    return AppConfig(
        project_dir=project_dir,
        profiles_dir=profiles_dir,
        target=target,
        profile_name=profile_name,
        adapter_type=adapter_type,
        project_id=_plain(project_id),
        dataset=_plain(dataset),
        location=_plain(output.get("location", "US")),
        method=method,
        keyfile=keyfile,
        host=_plain(output.get("host")) if output.get("host") is not None else None,
        port=int(_plain(output.get("port", 5432))) if adapter_type == "postgres" else None,
        user=_plain(output.get("user")) if output.get("user") is not None else None,
        password=_plain(password) if password is not None else None,
        dbname=_plain(output.get("dbname")) if output.get("dbname") is not None else None,
        sslmode=_plain(output.get("sslmode")) if output.get("sslmode") is not None else None,
        archive_cache_days=archive_cache_days,
        target_path=project_dir / project.get("target-path", "target"),
        lock_timeout_seconds=lock_timeout_seconds,
        max_cache_issues=int(os.environ.get("DBT_DQM_MAX_CACHE_ISSUES", str(MAX_CACHE_ISSUES))),
        max_cache_mib=int(os.environ.get("DBT_DQM_MAX_CACHE_MIB", str(MAX_CACHE_MIB))),
    )
