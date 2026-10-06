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

import psycopg2
from google.cloud import bigquery
from google.oauth2 import service_account
from psycopg2.extras import RealDictCursor

from .config import AppConfig
from .store import EDITABLE_FIELDS, Patch


def validate_dbt(config: AppConfig) -> None:
    environment_dbt = str(Path(sys.executable).with_name("dbt"))
    dbt_executable = environment_dbt if Path(environment_dbt).exists() else "dbt"
    base = [
        "--project-dir",
        str(config.project_dir),
        "--profiles-dir",
        str(config.profiles_dir),
        "--target",
        config.target,
    ]
    subprocess.run([dbt_executable, "debug", *base], check=True, capture_output=True, text=True)
    subprocess.run([dbt_executable, "parse", *base], check=True, capture_output=True, text=True)


def client_for(config: AppConfig) -> bigquery.Client:
    if config.adapter_type != "bigquery":
        raise ValueError("BigQuery client requested for a non-BigQuery profile")
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


def _relation_parts(config: AppConfig, name: str) -> tuple[str | None, str, str]:
    manifest_path = (config.target_path or (config.project_dir / "target")) / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        nodes = manifest.get("nodes", {})
        node = nodes.get(f"model.dbt_dqm.{name}")
        if node is None:
            node = next(
                (
                    candidate
                    for candidate in nodes.values()
                    if candidate.get("resource_type") == "model"
                    and candidate.get("package_name") == "dbt_dqm"
                    and (candidate.get("alias") or candidate.get("name")) == name
                ),
                None,
            )
        if node:
            return node.get("database"), node["schema"], node.get("alias") or name
    return config.project_id, config.dataset, name


def _table(config: AppConfig, name: str) -> str:
    database, schema, identifier = _relation_parts(config, name)
    if config.adapter_type == "bigquery":
        return f"`{database}.{schema}.{identifier}`"
    return f'"{schema}"."{identifier}"'


def fetch_issues(config: AppConfig) -> list[dict[str, Any]]:
    if config.archive_cache_days == 0:
        history_predicate = "record_status = 'Active'"
    elif config.adapter_type == "bigquery":
        history_predicate = (
            "(record_status = 'Active' or archived_at >= "
            f"timestamp_sub(current_timestamp(), interval {config.archive_cache_days} day))"
        )
    else:
        history_predicate = (
            "(record_status = 'Active' or archived_at >= current_timestamp - "
            f"interval '{config.archive_cache_days} days')"
        )
    query = (
        f"select * from {_table(config, 'dqm_all_issues')} "
        f"where {history_predicate} order by first_seen_at desc"
    )
    if config.adapter_type == "bigquery":
        rows = [dict(row.items()) for row in client_for(config).query(query).result()]
    else:
        with (
            _postgres_connection(config) as connection,
            connection.cursor(cursor_factory=RealDictCursor) as cursor,
        ):
            cursor.execute(query)
            rows = [dict(row) for row in cursor.fetchall()]
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
    if config.adapter_type == "postgres":
        return _apply_postgres_patches(config, patches)
    return _apply_bigquery_patches(config, patches)


def _apply_bigquery_patches(config: AppConfig, patches: Iterable[Patch]) -> str:
    patches = list(patches)
    if not patches:
        return ""
    invalid = {patch.field_name for patch in patches} - set(EDITABLE_FIELDS)
    if invalid:
        raise ValueError(f"Unsupported fields: {sorted(invalid)}")
    batch_id = _batch_id(patches)
    client = client_for(config)
    existing_job = client.query(
        f"select occurrence_id, field_name from {_table(config, 'dqm_annotation_changes')} "
        "where batch_id=@batch_id",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("batch_id", "STRING", batch_id)]
        ),
    )
    existing = {(row["occurrence_id"], row["field_name"]) for row in existing_job.result()}
    expected = {(patch.occurrence_id, patch.field_name) for patch in patches}
    if existing == expected:
        return batch_id
    if existing:
        raise RuntimeError("Incomplete prior annotation batch; synchronize before retrying.")
    staging_database, staging_schema, _ = _relation_parts(config, "dqm_issue_occurrences")
    staging_id = f"{staging_database}.{staging_schema}.dqm_app_change_staging"
    client.query(
        f"""
        create table if not exists `{staging_id}` (
          batch_id string, occurrence_id string, field_name string,
          old_value string, new_value string, changed_at timestamp, changed_by string,
          base_annotation_version int64
        )
        """
    ).result()
    client.query(
        f"alter table `{staging_id}` add column if not exists base_annotation_version int64"
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
            bigquery.SchemaField("base_annotation_version", "INT64"),
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
    workflow_assignment = next(
        assignment for assignment in assignments if assignment.startswith("workflow_status =")
    )
    assignments.append(workflow_assignment.replace("workflow_status =", "test_status =", 1))
    server_old_value = (
        "case s.field_name "
        + " ".join(f"when '{field}' then o.{field}" for field in EDITABLE_FIELDS)
        + " end"
    )
    sql = f"""
      begin transaction;
      assert not exists (
        select 1
        from `{staging_id}` s
        left join {_table(config, "dqm_issue_occurrences")} o using (occurrence_id)
        where s.batch_id=@batch_id
          and (o.occurrence_id is null
               or coalesce(o.annotation_version, 0) != s.base_annotation_version)
      ) as 'Annotation conflict: synchronize and review the newer warehouse values.';
      merge {_table(config, "dqm_annotation_changes")} t
      using (
        select
          s.batch_id, s.occurrence_id, s.field_name,
          {server_old_value} as old_value,
          s.new_value, s.changed_at, s.changed_by,
          coalesce(o.annotation_version, 0) as base_annotation_version,
          coalesce(o.annotation_version, 0) + 1 as resulting_annotation_version
        from (
          select * except(row_number) from (
            select *, row_number() over (
              partition by batch_id, occurrence_id, field_name order by changed_at desc
            ) as row_number
            from `{staging_id}` where batch_id = @batch_id
          ) where row_number = 1
        ) s
        join {_table(config, "dqm_issue_occurrences")} o using (occurrence_id)
      ) s
        on t.batch_id=s.batch_id and t.occurrence_id=s.occurrence_id and t.field_name=s.field_name
      when not matched then insert(batch_id, occurrence_id, field_name, old_value, new_value,
                                   changed_at, changed_by, base_annotation_version,
                                   resulting_annotation_version)
        values(s.batch_id, s.occurrence_id, s.field_name, s.old_value, s.new_value,
               s.changed_at, s.changed_by, s.base_annotation_version,
               s.resulting_annotation_version);
      update {_table(config, "dqm_issue_occurrences")} t
      set {", ".join(assignments)}, annotation_updated_at=current_timestamp(),
          annotation_version=coalesce(annotation_version, 0) + 1
      where occurrence_id in (select occurrence_id from `{staging_id}` where batch_id=@batch_id);
      delete from `{staging_id}` where batch_id=@batch_id;
      commit transaction;
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("batch_id", "STRING", batch_id)]
    )
    client.query(sql, job_config=job_config).result()
    return batch_id


def _postgres_connection(config: AppConfig):
    parameters = {
        "host": config.host,
        "port": config.port,
        "user": config.user,
        "password": config.password,
        "dbname": config.dbname,
    }
    if config.sslmode:
        parameters["sslmode"] = config.sslmode
    return psycopg2.connect(
        **{key: value for key, value in parameters.items() if value is not None}
    )


def _apply_postgres_patches(config: AppConfig, patches: list[Patch]) -> str:
    if not patches:
        return ""
    invalid = {patch.field_name for patch in patches} - set(EDITABLE_FIELDS)
    if invalid:
        raise ValueError(f"Unsupported fields: {sorted(invalid)}")
    batch_id = _batch_id(patches)
    occurrence_table = _table(config, "dqm_issue_occurrences")
    audit_table = _table(config, "dqm_annotation_changes")
    by_occurrence: dict[str, list[Patch]] = {}
    for patch in patches:
        by_occurrence.setdefault(patch.occurrence_id, []).append(patch)

    with (
        _postgres_connection(config) as connection,
        connection.cursor(cursor_factory=RealDictCursor) as cursor,
    ):
        cursor.execute(
            f"select occurrence_id, field_name from {audit_table} where batch_id=%s",
            (batch_id,),
        )
        existing = {(row["occurrence_id"], row["field_name"]) for row in cursor.fetchall()}
        expected = {(patch.occurrence_id, patch.field_name) for patch in patches}
        if existing == expected:
            return batch_id
        if existing:
            raise RuntimeError("Incomplete prior annotation batch; synchronize before retrying.")

        for occurrence_id, occurrence_patches in by_occurrence.items():
            cursor.execute(
                f"select * from {occurrence_table} where occurrence_id=%s for update",
                (occurrence_id,),
            )
            occurrence = cursor.fetchone()
            if occurrence is None:
                raise RuntimeError(f"Annotation conflict: occurrence {occurrence_id} is missing.")
            current_version = int(occurrence.get("annotation_version") or 0)
            if any(
                patch.base_annotation_version != current_version for patch in occurrence_patches
            ):
                raise RuntimeError(
                    "Annotation conflict: synchronize and review newer warehouse values."
                )
            resulting_version = current_version + 1
            assignments = []
            values: list[Any] = []
            for patch in occurrence_patches:
                server_old_value = occurrence.get(patch.field_name)
                cursor.execute(
                    f"insert into {audit_table} "
                    "(batch_id, occurrence_id, field_name, old_value, new_value, changed_at, "
                    "changed_by, base_annotation_version, resulting_annotation_version) "
                    "values (%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict do nothing",
                    (
                        batch_id,
                        occurrence_id,
                        patch.field_name,
                        server_old_value,
                        patch.new_value,
                        patch.changed_at,
                        getpass.getuser(),
                        current_version,
                        resulting_version,
                    ),
                )
                assignments.append(f'"{patch.field_name}"=%s')
                values.append(patch.new_value)
                if patch.field_name == "workflow_status":
                    assignments.append('"test_status"=%s')
                    values.append(patch.new_value)
            assignments.extend(["annotation_updated_at=current_timestamp", "annotation_version=%s"])
            values.extend([resulting_version, occurrence_id])
            cursor.execute(
                f"update {occurrence_table} set {', '.join(assignments)} where occurrence_id=%s",
                values,
            )
    return batch_id


def sync_worker(config: AppConfig) -> list[dict[str, Any]]:
    validate_dbt(config)
    return fetch_issues(config)


def apply_worker(config: AppConfig, patches: list[Patch]) -> tuple[str, list[dict[str, Any]]]:
    validate_dbt(config)
    batch_id = apply_patches(config, patches)
    return batch_id, fetch_issues(config)
