from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dbt.cli.resolvers import default_profiles_dir, default_project_dir
from dbt.clients.jinja import get_rendered
from dbt.config.profile import Profile
from dbt.config.renderer import ProfileRenderer
from dbt.context.base import generate_base_context
from dbt_common.context import set_invocation_context
from platformdirs import user_data_path

from .errors import AppError
from .limits import MAX_CACHE_ISSUES, MAX_CACHE_MIB, validate_limits

BROWSE_MODES = ("snapshot", "warehouse", "auto")


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
    # Ceiling for one BigQuery search in warehouse browsing: estimated with a dry run first and
    # enforced with maximum_bytes_billed on the search query itself.
    max_search_gib: float = 1.0
    # snapshot: sync the whole review population locally; warehouse: read pages on demand;
    # auto: snapshot while the population fits the cache limits, warehouse paging above them.
    browse_mode: str = "auto"

    # None preserves explicit flags for programmatically constructed configurations.
    dbt_cli_args: tuple[str, ...] | None = None
    working_dir: Path | None = None

    def __post_init__(self) -> None:
        validate_limits(self.max_cache_issues, self.max_cache_mib)
        if not 0 < self.max_search_gib <= 1024:
            raise ValueError("--max-search-gib must be greater than 0 and at most 1024")
        if self.browse_mode not in BROWSE_MODES:
            raise ValueError(f"--browse must be one of {', '.join(BROWSE_MODES)}")

    @property
    def max_search_bytes(self) -> int:
        return int(self.max_search_gib * 2**30)

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


def resolve_project_dir(value: str | Path | None = None) -> Path:
    return _path(value if value is not None else os.environ.get("DBT_PROJECT_DIR")
                 or default_project_dir())


def resolve_profiles_dir(value: str | Path | None = None) -> Path:
    return _path(value if value is not None else os.environ.get("DBT_PROFILES_DIR")
                 or default_profiles_dir())


def _path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def _read_yaml(path: Path) -> dict:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as error:
        raise AppError(f"Could not read {path.name}. Check its location and YAML syntax.",
                       f"Path: {path}\nError type: {type(error).__name__}") from None
    if not isinstance(value, dict):
        raise AppError(f"{path.name} must contain a YAML mapping.")
    return value


def _profile_name(project: dict, cli_vars=None) -> str:
    override = os.environ.get("DBT_PROFILE")
    if override:
        return override
    name = project.get("profile")
    if name is None:
        raise AppError("The dbt project does not define a profile. Set DBT_PROFILE or profile.")
    return _plain(name, cli_vars)


def _selected_profile(profiles_dir: Path, profile_name: str, target: str | None, cli_vars=None):
    profiles = _read_yaml(profiles_dir / "profiles.yml")
    if profile_name not in profiles:
        raise AppError("The selected dbt profile was not found in profiles.yml.")
    set_invocation_context(os.environ)
    try:
        return Profile.render_profile(profiles[profile_name], profile_name,
                                      target if target is not None else os.environ.get("DBT_TARGET") or None,
                                      ProfileRenderer(cli_vars or {}))
    except Exception as error:  # noqa: BLE001 - normalize dbt rendering failures for the UI.
        raise AppError("Could not resolve the dbt profile or target. Check profiles.yml and "
                       "its environment variables.", str(error)) from None


def resolve_workspace_path(project_dir: str | Path | None = None, target: str | None = None,
                           profiles_dir: str | Path | None = None) -> Path:
    project_dir = resolve_project_dir(project_dir)
    profile_name = _profile_name(_read_yaml(project_dir / "dbt_project.yml"))
    target = target if target is not None else os.environ.get("DBT_TARGET") or None
    if target is None:
        target, _ = _selected_profile(resolve_profiles_dir(profiles_dir), profile_name, None)
    return workspace_path_for(project_dir, profile_name, target)


def _plain(value: object, cli_vars=None) -> str:
    set_invocation_context(os.environ)
    rendered = get_rendered(str(value), generate_base_context(cli_vars or {}), native=True)
    return str(rendered)


def load_config(project_dir: str | Path | None = None, profiles_dir: str | Path | None = None,
                target: str | None = None, *, cli_vars=None) -> AppConfig:
    working_dir = Path.cwd()
    explicit = (project_dir, profiles_dir, target)
    project_dir = resolve_project_dir(project_dir)
    profiles_dir = resolve_profiles_dir(profiles_dir)
    project = _read_yaml(project_dir / "dbt_project.yml")
    profile_name = _profile_name(project, cli_vars)
    target, output = _selected_profile(profiles_dir, profile_name, target, cli_vars)
    cli_args = []
    for flag, value, resolved in zip(("--project-dir", "--profiles-dir", "--target"), explicit,
                                     (project_dir, profiles_dir, target), strict=True):
        if value is not None:
            cli_args.extend((flag, str(resolved)))
    adapter_type = str(output.get("type"))
    if adapter_type not in {"bigquery", "postgres"}:
        raise ValueError("The local app supports BigQuery and Postgres profiles")
    method = output.get("method", "oauth")
    keyfile = (
        Path(str(output["keyfile"])).expanduser()
        if adapter_type == "bigquery" and method == "service-account"
        else None
    )
    variables = project.get("vars", {}) or {}
    dqm_vars = variables.get("dbt_dqm", {}) if isinstance(variables.get("dbt_dqm"), dict) else {}
    custom_dataset = dqm_vars.get("schema") or variables.get("dbt_dqm_schema")
    base_schema = output.get("dataset") if adapter_type == "bigquery" else output.get("schema")
    custom_dataset = _plain(custom_dataset, cli_vars) if custom_dataset is not None else None
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
        project_id=str(project_id),
        dataset=str(dataset),
        location=str(output.get("location", "US")),
        method=method,
        keyfile=keyfile,
        host=str(output.get("host")) if output.get("host") is not None else None,
        port=int(output.get("port", 5432)) if adapter_type == "postgres" else None,
        user=str(output.get("user")) if output.get("user") is not None else None,
        password=str(password) if password is not None else None,
        dbname=str(output.get("dbname")) if output.get("dbname") is not None else None,
        sslmode=str(output.get("sslmode")) if output.get("sslmode") is not None else None,
        archive_cache_days=archive_cache_days,
        target_path=project_dir / Path(os.environ.get("DBT_TARGET_PATH")
                                       or _plain(project.get("target-path", "target"), cli_vars)).expanduser(),
        dbt_cli_args=tuple(cli_args),
        working_dir=working_dir,
        lock_timeout_seconds=lock_timeout_seconds,
        max_cache_issues=int(os.environ.get("DBT_DQM_MAX_CACHE_ISSUES", str(MAX_CACHE_ISSUES))),
        max_cache_mib=int(os.environ.get("DBT_DQM_MAX_CACHE_MIB", str(MAX_CACHE_MIB))),
        max_search_gib=float(os.environ.get("DBT_DQM_MAX_SEARCH_GIB", "1")),
        browse_mode=os.environ.get("DBT_DQM_BROWSE", "auto"),
    )
