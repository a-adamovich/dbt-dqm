from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from platformdirs import user_data_path


@dataclass(frozen=True)
class AppConfig:
    project_dir: Path
    profiles_dir: Path
    target: str
    profile_name: str
    project_id: str
    dataset: str
    location: str
    method: str
    keyfile: Path | None

    @property
    def workspace_path(self) -> Path:
        identity = f"{self.project_dir.resolve()}|{self.profile_name}|{self.target}"
        suffix = hashlib.sha256(identity.encode()).hexdigest()[:16]
        return user_data_path("dbt-dqm") / suffix / "workspace.sqlite"


def _plain(value: object) -> str:
    text = str(value)
    if "{{" in text:
        raise ValueError("Jinja expressions in profiles.yml are not supported by the v1 app")
    return os.path.expandvars(os.path.expanduser(text))


def load_config(project_dir: str | Path, profiles_dir: str | Path, target: str) -> AppConfig:
    project_dir = Path(project_dir).expanduser().resolve()
    profiles_dir = Path(profiles_dir).expanduser().resolve()
    project = yaml.safe_load((project_dir / "dbt_project.yml").read_text())
    profile_name = project["profile"]
    profiles = yaml.safe_load((profiles_dir / "profiles.yml").read_text())
    output = profiles[profile_name]["outputs"][target]
    if output.get("type") != "bigquery":
        raise ValueError("dbt-dqm v1 supports BigQuery profiles only")
    method = output.get("method", "oauth")
    keyfile = Path(_plain(output["keyfile"])) if method == "service-account" else None
    variables = project.get("vars", {}) or {}
    dqm_vars = variables.get("dbt_dqm", {}) if isinstance(variables.get("dbt_dqm"), dict) else {}
    custom_dataset = dqm_vars.get("schema") or variables.get("dbt_dqm_schema")
    dataset = f"{output['dataset']}_{custom_dataset}" if custom_dataset else output["dataset"]
    return AppConfig(
        project_dir=project_dir,
        profiles_dir=profiles_dir,
        target=target,
        profile_name=profile_name,
        project_id=_plain(output["project"]),
        dataset=_plain(dataset),
        location=_plain(output.get("location", "US")),
        method=method,
        keyfile=keyfile,
    )
