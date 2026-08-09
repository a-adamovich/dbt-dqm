from __future__ import annotations

import getpass
import json
import subprocess
import sys
import uuid
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from google.cloud import bigquery
from google.oauth2 import service_account

from .config import AppConfig
from .store import EDITABLE_FIELDS, Patch


def validate_dbt(config: AppConfig) -> None:
    environment_dbt = str(Path(sys.executable).with_name("dbt"))
    dbt_executable = environment_dbt if Path(environment_dbt).exists() else "dbt"
    command = [
        dbt_executable, "debug", "--project-dir", str(config.project_dir),
        "--profiles-dir", str(config.profiles_dir), "--target", config.target,
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)


def client_for(config: AppConfig) -> bigquery.Client:
    if config.method == "service-account":
        if config.keyfile is None:
            raise ValueError("The dbt profile does not define keyfile")
        credentials = service_account.Credentials.from_service_account_file(config.keyfile)
        return bigquery.Client(
            project=config.project_id, credentials=credentials, location=config.location
        )
    if config.method == "oauth":
        return bigquery.Client(project=config.project_id, location=config.location)
    raise ValueError(f"Unsupported BigQuery authentication method: {config.method}")


def _table(config: AppConfig, name: str) -> str:
    return f"`{config.project_id}.{config.dataset}.{name}`"


def fetch_issues(config: AppConfig) -> list[dict[str, Any]]:
    query = f"select * from {_table(config, 'dqm_all_issues')} order by first_seen_at desc"
    rows = [dict(row.items()) for row in client_for(config).query(query).result()]
    _validate_issue_rows(rows)
    return rows


def _json_object(value: Any, *, required: bool) -> dict[str, Any] | None:
    """Validate portable record data without accepting the retired array representation."""
    if value is None:
        if required:
            raise ValueError("required JSON object is null")
        return None
    parsed = value if isinstance(value, dict) else json.loads(str(value))
    if not isinstance(parsed, dict):
        raise TypeError("record payload is not a JSON object")
    if required and not parsed:
        raise ValueError("required JSON object is empty")
    return parsed


def _validate_issue_rows(rows: list[dict[str, Any]]) -> None:
    """Reject an incompatible warehouse interface instead of rendering empty issue cards."""
    seen: set[str] = set()
    for row in rows:
        occurrence_id = str(row.get("occurrence_id") or "")
        test_name = str(row.get("test_name") or "unnamed test")
        if not occurrence_id:
            raise ValueError(f"{test_name}: occurrence_id is missing")
        if occurrence_id in seen:
            raise ValueError(f"Duplicate occurrence_id returned by dqm_all_issues: {occurrence_id}")
        seen.add(occurrence_id)
        try:
            _json_object(row.get("record_values_json"), required=True)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError(
                f"{test_name}: incompatible record JSON. Run the tracked tests and rebuild "
                "package:dbt_dqm before synchronizing."
            ) from error


def _batch_id(patches: Iterable[Patch]) -> str:
    """Return a stable ID so retrying the same frozen patch set is idempotent."""
    identity = [
        asdict(patch)
        for patch in sorted(
            patches,
            key=lambda patch: (patch.occurrence_id, patch.field_name, patch.version),
        )
    ]
    serialized = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, serialized))


def apply_patches(config: AppConfig, patches: Iterable[Patch]) -> str:
    patches = list(patches)
    if not patches:
        return ""
    invalid = {patch.field_name for patch in patches} - set(EDITABLE_FIELDS)
    if invalid:
        raise ValueError(f"Unsupported fields: {sorted(invalid)}")
    batch_id = _batch_id(patches)
    client = client_for(config)
    staging_id = f"{config.project_id}.{config.dataset}.dqm_app_change_staging"
    client.query(
        f"""
        create table if not exists `{staging_id}` (
          batch_id string, occurrence_id string, field_name string,
          old_value string, new_value string, changed_at timestamp, changed_by string
        )
        """
    ).result()
    payload = [
        {
            "batch_id": batch_id,
            **asdict(patch),
            "changed_by": getpass.getuser(),
        }
        for patch in patches
    ]
    for row in payload:
        row.pop("version", None)
    load_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        schema=[
            bigquery.SchemaField("batch_id", "STRING"),
            bigquery.SchemaField("occurrence_id", "STRING"),
            bigquery.SchemaField("field_name", "STRING"),
            bigquery.SchemaField("old_value", "STRING"),
            bigquery.SchemaField("new_value", "STRING"),
            bigquery.SchemaField("changed_at", "TIMESTAMP"),
            bigquery.SchemaField("changed_by", "STRING"),
        ],
    )
    client.load_table_from_json(payload, staging_id, job_config=load_config).result()

    assignments = []
    for field in EDITABLE_FIELDS:
        assignments.append(
            f"{field} = if(exists(select 1 from `{staging_id}` b where b.batch_id=@batch_id "
            f"and b.occurrence_id=t.occurrence_id and b.field_name='{field}'), "
            f"(select any_value(new_value) from `{staging_id}` b where b.batch_id=@batch_id "
            f"and b.occurrence_id=t.occurrence_id and b.field_name='{field}'), t.{field})"
        )
    sql = f"""
      begin transaction;
      merge {_table(config, 'dqm_annotation_changes')} t
      using (
        select * except(row_number) from (
          select *, row_number() over (
            partition by batch_id, occurrence_id, field_name order by changed_at desc
          ) as row_number
          from `{staging_id}` where batch_id = @batch_id
        ) where row_number = 1
      ) s
        on t.batch_id=s.batch_id and t.occurrence_id=s.occurrence_id and t.field_name=s.field_name
      when not matched then insert(batch_id, occurrence_id, field_name, old_value, new_value,
                                   changed_at, changed_by)
        values(s.batch_id, s.occurrence_id, s.field_name, s.old_value, s.new_value,
               s.changed_at, s.changed_by);
      update {_table(config, 'dqm_issue_occurrences')} t
      set {', '.join(assignments)}, annotation_updated_at=current_timestamp()
      where occurrence_id in (select occurrence_id from `{staging_id}` where batch_id=@batch_id);
      delete from `{staging_id}` where batch_id=@batch_id;
      commit transaction;
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("batch_id", "STRING", batch_id)]
    )
    client.query(sql, job_config=job_config).result()
    return batch_id


def sync_worker(config: AppConfig) -> list[dict[str, Any]]:
    validate_dbt(config)
    return fetch_issues(config)


def apply_worker(config: AppConfig, patches: list[Patch]) -> tuple[str, list[dict[str, Any]]]:
    validate_dbt(config)
    batch_id = apply_patches(config, patches)
    return batch_id, fetch_issues(config)
