from __future__ import annotations

import getpass
import hashlib
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import google.auth
import psycopg2
from google.api_core.exceptions import Conflict, NotFound
from google.cloud import bigquery
from google.oauth2 import service_account
from psycopg2.extras import RealDictCursor

from .config import AppConfig
from .errors import AppError, SnapshotUnavailable, classify
from .limits import check_cache_size, issue_json
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
    environment = os.environ.copy()
    if config.credentials_file is not None:
        environment["GOOGLE_APPLICATION_CREDENTIALS"] = str(config.credentials_file.resolve())
    subprocess.run([dbt_executable, "debug", *base], check=True, capture_output=True, text=True,
                   env=environment)
    subprocess.run([dbt_executable, "parse", *base], check=True, capture_output=True, text=True,
                   env=environment)


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
        if config.credentials_file is not None:
            credentials, _ = google.auth.load_credentials_from_file(
                str(config.credentials_file), scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            return bigquery.Client(
                project=config.project_id, credentials=credentials, location=config.location
            )
        return bigquery.Client(project=config.project_id, location=config.location)
    raise ValueError(f"Unsupported BigQuery authentication method: {config.method}")


def _relation_parts(config: AppConfig, name: str) -> tuple[str | None, str, str]:
    manifest_path = (config.target_path or (config.project_dir / "target")) / "manifest.json"
    if manifest_path.exists():
        nodes = manifest_nodes(config)
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
        reconcile = nodes.get("model.dbt_dqm.dqm_reconcile")
        if reconcile and name.startswith("dqm_"):
            return reconcile.get("database"), reconcile["schema"], name
    return config.project_id, config.dataset, name


def _table(config: AppConfig, name: str) -> str:
    database, schema, identifier = _relation_parts(config, name)
    if config.adapter_type == "bigquery":
        return f"`{database}.{schema}.{identifier}`"
    return f'"{schema}"."{identifier}"'


def _query_rows(config: AppConfig, query: str) -> list[dict[str, Any]]:
    if config.adapter_type == "bigquery":
        return [dict(row.items()) for row in client_for(config).query(query).result()]
    with (
        _postgres_connection(config) as connection,
        connection.cursor(cursor_factory=RealDictCursor) as cursor,
    ):
        cursor.execute(query)
        return [dict(row) for row in cursor.fetchall()]


def _issue_predicate(config: AppConfig) -> str:
    """Rows the local cache holds: Active issues plus recently archived history.

    The archive cutoff is fixed once per sync, so the view rows and the direct table count
    below are filtered identically even when a row is close to the boundary.
    """
    if config.archive_cache_days == 0:
        return "record_status = 'Active'"
    cutoff = (datetime.now(UTC) - timedelta(days=config.archive_cache_days)).isoformat()
    literal = (
        f"timestamp '{cutoff}'" if config.adapter_type == "bigquery" else f"timestamptz '{cutoff}'"
    )
    return f"(record_status = 'Active' or archived_at >= {literal})"


def _iter_issue_rows(config: AppConfig, query: str):
    """Only issue downloads stream; small metadata/Health queries retain the list API."""
    if config.adapter_type == "bigquery":
        result = client_for(config).query(query).result(page_size=500)
        for page in result.pages:
            for row in page:
                yield dict(row.items())
        return
    with (_postgres_connection(config) as connection,
          connection.cursor(name="dqm_sync_" + uuid.uuid4().hex,
                            cursor_factory=RealDictCursor) as cursor):
        cursor.itersize = 500
        cursor.execute(query)
        while page := cursor.fetchmany(500):
            for row in page:
                yield dict(row)


def _snapshot_state(config: AppConfig, predicate: str) -> dict[str, Any]:
    control = _table(config, "dqm_reconciliation_control")
    occurrences = _table(config, "dqm_issue_occurrences")
    return _query_rows(
        config,
        f"select (select max(generation) from {control}) as generation, "
        f"(select max(setup_status) from {control}) as setup_status, "
        f"(select count(*) from {occurrences} where {predicate}) as issue_count",
    )[0]


def fetch_issues(config: AppConfig) -> list[dict[str, Any]]:
    """Read the issue view and verify it before it may replace the local cache.

    Every reconciliation, migration and recovery operation bumps the control generation, so an
    unchanged generation around the read means the rows come from one tracking state. A view
    whose row count disagrees with the tracking table (stale or redefined views, wrong schema) is
    rejected instead of silently emptying the cache; a verified empty result is accepted.
    """
    predicate = _issue_predicate(config)
    before = _snapshot_state(config, predicate)
    if before["setup_status"] != "ready":
        raise SnapshotUnavailable(
            "dbt-dqm setup or a migration is in progress, so warehouse data isn't ready yet. "
            "Showing cached data; sync again shortly."
        )
    check_cache_size(before["issue_count"], 0, config.max_cache_issues, config.max_cache_bytes)
    downloaded = _iter_issue_rows(
        config,
        f"select * from {_table(config, 'dqm_all_issues')} "
        f"where {predicate} order by first_seen_at desc, occurrence_id "
        f"limit {config.max_cache_issues + 1}",
    )
    rows = []
    size = 0
    try:
        for row in downloaded:
            size += len(issue_json(row).encode("utf-8"))
            check_cache_size(len(rows) + 1, size, config.max_cache_issues, config.max_cache_bytes)
            rows.append(row)
    finally:
        downloaded.close()
    after = _snapshot_state(config, predicate)
    if after["generation"] != before["generation"]:
        raise SnapshotUnavailable(
            "The warehouse changed while syncing (a reconciliation finished). "
            "Showing cached data; sync again."
        )
    if after["setup_status"] != "ready":
        raise SnapshotUnavailable("DQM setup changed while syncing. Cached data is kept.")
    if len(rows) != after["issue_count"]:
        raise SnapshotUnavailable(
            f"dqm_all_issues returned {len(rows)} issues but the tracking table holds "
            f"{after['issue_count']}. The public views may be out of date; rebuild "
            "package:dbt_dqm, then sync again. Showing cached data."
        )
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
    patches = _validated_patches(patches)
    if any(
        patch.field_name == "review_verdict"
        and patch.new_value not in {"UNREVIEWED", "TRUE_POSITIVE", "FALSE_POSITIVE"}
        for patch in patches
    ):
        raise ValueError("Invalid review verdict")
    if config.adapter_type == "postgres":
        return _apply_postgres_patches(config, patches)
    return _apply_bigquery_patches(config, patches)


def _validated_patches(patches: Iterable[Patch]) -> list[Patch]:
    canonical = {}
    versions = {}
    for patch in patches:
        if patch.field_name not in EDITABLE_FIELDS or not patch.occurrence_id:
            raise ValueError("Unsupported annotation field or missing occurrence ID")
        if (isinstance(patch.base_annotation_version, bool)
                or not isinstance(patch.base_annotation_version, int)
                or patch.base_annotation_version < 0):
            raise ValueError("Invalid annotation base version")
        key = patch.occurrence_id, patch.field_name
        if key in canonical and canonical[key] != patch:
            raise ValueError("Conflicting duplicate annotation patches")
        if versions.setdefault(patch.occurrence_id, patch.base_annotation_version) != patch.base_annotation_version:
            raise ValueError("Inconsistent annotation base versions")
        canonical[key] = patch
    return list(canonical.values())


def _apply_bigquery_patches(config: AppConfig, patches: Iterable[Patch]) -> str:
    patches = list(patches)
    if not patches:
        return ""
    batch_id = _batch_id(patches)
    upload_id = uuid.uuid4().hex
    client = client_for(config)
    database, schema, _ = _relation_parts(config, "dqm_issue_occurrences")
    staging_id = f"{database}.{schema}.dqm_app_change_staging"
    try:
        table = client.get_table(staging_id)
    except NotFound as error:
        raise AppError("App staging is missing. Build package:dbt_dqm before applying.") from error
    if not {"upload_id", "staged_at"} <= {field.name for field in table.schema}:
        raise AppError("App staging requires migration 0004. Build package:dbt_dqm, "
                       "then restart reviewer apps before applying.")
    payload = []
    for patch in patches:
        row = {"batch_id": batch_id, "upload_id": upload_id,
               **asdict(patch), "changed_by": getpass.getuser()}
        row.pop("version")
        payload.append(row)
    # Omitting staged_at uses its CURRENT_TIMESTAMP() warehouse default. A load has its own
    # job ID; uncertain uploads remain isolated from every later attempt until cleanup.
    load_config = bigquery.LoadJobConfig(
        create_disposition=bigquery.CreateDisposition.CREATE_NEVER,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        schema=[bigquery.SchemaField(name, kind) for name, kind in (
            ("batch_id", "STRING"), ("upload_id", "STRING"), ("occurrence_id", "STRING"),
            ("field_name", "STRING"), ("old_value", "STRING"), ("new_value", "STRING"),
            ("changed_at", "TIMESTAMP"), ("changed_by", "STRING"),
            ("base_annotation_version", "INT64"))],
    )
    client.load_table_from_json(payload, staging_id, job_config=load_config,
                                job_id="dqm_annotation_upload_" + upload_id).result()
    parameters = [bigquery.ScalarQueryParameter("batch_id", "STRING", batch_id),
                  bigquery.ScalarQueryParameter("upload_id", "STRING", upload_id),
                  bigquery.ScalarQueryParameter("patch_count", "INT64", len(patches))]
    client.query(_bigquery_annotation_apply_sql(config), job_config=bigquery.QueryJobConfig(
        query_parameters=parameters)).result()
    return batch_id


def _bigquery_annotation_apply_sql(config: AppConfig) -> str:
    staging = _table(config, "dqm_app_change_staging")
    occurrences = _table(config, "dqm_issue_occurrences")
    audit = _table(config, "dqm_annotation_changes")
    assignments = []
    for field in EDITABLE_FIELDS:
        assignments.append(
            f"{field} = if(exists(select 1 from attempt b where b.occurrence_id=t.occurrence_id "
            f"and b.field_name='{field}'), "
            f"(select new_value from attempt b where b.occurrence_id=t.occurrence_id "
            f"and b.field_name='{field}'), t.{field})"
        )
    workflow = next(a for a in assignments if a.startswith("workflow_status ="))
    assignments.append(workflow.replace("workflow_status =", "test_status =", 1))
    old_value = "case s.field_name " + " ".join(
        f"when '{field}' then o.{field}" for field in EDITABLE_FIELDS) + " end"
    fields = ",".join(f"'{field}'" for field in EDITABLE_FIELDS)
    return f"""
      declare audited_count int64;
      begin transaction;
      create temp table attempt as
        select * from {staging} where upload_id=@upload_id and batch_id=@batch_id;
      assert (select count(*) from attempt)=@patch_count
        as 'Missing or duplicate annotation upload; retry with a new upload.';
      assert not exists(select 1 from attempt group by occurrence_id,field_name having count(*)>1)
        as 'Conflicting duplicate annotation patches.';
      assert not exists(select 1 from attempt where occurrence_id is null or field_name is null
        or field_name not in ({fields}) or base_annotation_version is null
        or base_annotation_version<0)
        as 'Invalid annotation upload.';
      assert not exists(select 1 from attempt group by occurrence_id
        having count(distinct base_annotation_version)>1)
        as 'Inconsistent annotation base versions.';
      set audited_count=(select count(*) from {audit} where batch_id=@batch_id);
      if audited_count>0 then
        assert audited_count=@patch_count and not exists (
          select 1 from attempt s left join {audit} a
            on a.batch_id=@batch_id and a.occurrence_id=s.occurrence_id and a.field_name=s.field_name
          where a.occurrence_id is null or a.new_value is distinct from s.new_value
            or a.base_annotation_version is distinct from s.base_annotation_version
        ) as 'Incomplete prior annotation batch; synchronize before retrying.';
      else
        assert not exists(select 1 from attempt where staged_at is null
          or staged_at <= timestamp_sub(current_timestamp(),interval 24 hour)
          or staged_at>current_timestamp())
          as 'Annotation upload expired; retry from your local pending edits.';
        assert not exists (
          select 1 from attempt s left join {occurrences} o using(occurrence_id)
          where o.occurrence_id is null
            or coalesce(o.annotation_version,0)!=s.base_annotation_version
        ) as 'Annotation conflict: synchronize and review the newer warehouse values.';
        insert into {audit} (batch_id,occurrence_id,field_name,old_value,new_value,changed_at,
                             changed_by,base_annotation_version,resulting_annotation_version)
          select s.batch_id,s.occurrence_id,s.field_name,{old_value},s.new_value,s.changed_at,
                 s.changed_by,coalesce(o.annotation_version,0),coalesce(o.annotation_version,0)+1
          from attempt s join {occurrences} o using(occurrence_id);
        update {occurrences} t
          set {", ".join(assignments)}, annotation_updated_at=current_timestamp(),
              annotation_version=coalesce(annotation_version,0)+1
          where occurrence_id in (select occurrence_id from attempt);
      end if;
      delete from {staging} where upload_id=@upload_id and batch_id=@batch_id;
      commit transaction;
    """


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
    # A reconciliation holds the occurrence table exclusively; fail fast instead of hanging the UI.
    parameters["options"] = f"-c lock_timeout={config.lock_timeout_seconds * 1000}"
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


def sync_worker(config: AppConfig) -> dict[str, Any]:
    validate_dbt(config)
    return {"issues": fetch_issues(config), "health": fetch_health(config)}


def apply_worker(
    config: AppConfig, patches: list[Patch]
) -> tuple[str, dict[str, Any] | None, AppError | None]:
    """Apply patches, then refresh. A failed refresh doesn't undo or hide a committed apply."""
    validate_dbt(config)
    batch_id = apply_patches(config, patches)
    try:
        return batch_id, {"issues": fetch_issues(config), "health": fetch_health(config)}, None
    except Exception as error:  # noqa: BLE001 - the apply already committed; report the refresh.
        return batch_id, None, classify(error)


MISSED_ROOT_CAUSES = ("no_test", "test_logic_gap", "threshold_too_loose", "test_not_run", "other")
MISSED_COLUMNS = (
    "missed_issue_id",
    "discovered_at",
    "reported_at",
    "reporter",
    "source_unique_id",
    "area_label",
    "test_unique_id",
    "description",
    "evidence_url",
    "priority",
    "detection_method",
    "root_cause",
)


def manifest_nodes(config: AppConfig) -> dict[str, dict[str, Any]]:
    path = (config.target_path or (config.project_dir / "target")) / "manifest.json"
    manifest = json.loads(path.read_text())
    return {**manifest.get("nodes", {}), **manifest.get("sources", {})}


def fetch_health(config: AppConfig) -> dict[str, Any]:
    health: dict[str, Any] = {"synced_at": datetime.now(UTC).isoformat()}
    for kind, table in (("tests", "dqm_test_health"), ("areas", "dqm_area_health")):
        health[kind] = _query_rows(config, f"select * from {_table(config, table)}")
    return health


def insert_missed_issue(config: AppConfig, issue: dict[str, Any]) -> str:
    """Reuse the caller's submission ID; a retry never creates another report."""
    if not issue.get("missed_issue_id") or not str(issue.get("description") or "").strip():
        raise ValueError("A stable submission ID and description are required")
    if not issue.get("source_unique_id") and not str(issue.get("area_label") or "").strip():
        raise ValueError("Select a source or describe an unmapped area")
    if issue.get("root_cause") not in MISSED_ROOT_CAUSES:
        raise ValueError("Invalid root cause")
    if issue.get("priority") not in (None, "critical", "high", "medium", "low"):
        raise ValueError("Invalid priority")
    values = {column: issue.get(column) for column in MISSED_COLUMNS}
    values["reporter"] = values["reporter"] or getpass.getuser()
    values["reported_at"] = values["reported_at"] or datetime.now(UTC)
    values["discovered_at"] = values["discovered_at"] or values["reported_at"]
    table = _table(config, "dqm_missed_issues")
    if config.adapter_type == "bigquery":
        params = [
            bigquery.ScalarQueryParameter(
                column,
                "TIMESTAMP" if column in ("reported_at", "discovered_at") else "STRING",
                values[column],
            )
            for column in MISSED_COLUMNS
        ]
        select = ",".join(f"@{column} as {column}" for column in MISSED_COLUMNS)
        _insert_missed_bigquery(
            config,
            table,
            values["missed_issue_id"],
            f"merge {table} target using (select {select}) source "
            "on target.missed_issue_id=source.missed_issue_id when not matched then insert "
            f"({','.join(MISSED_COLUMNS)}) values ({','.join('source.' + c for c in MISSED_COLUMNS)})",
            bigquery.QueryJobConfig(query_parameters=params),
        )
    else:
        with _postgres_connection(config) as connection, connection.cursor() as cursor:
            cursor.execute(
                f"insert into {table} ({','.join(MISSED_COLUMNS)}) "
                f"values ({','.join(['%s'] * len(MISSED_COLUMNS))}) "
                "on conflict(missed_issue_id) do nothing",
                [values[column] for column in MISSED_COLUMNS],
            )
    return str(values["missed_issue_id"])


def _insert_missed_bigquery(config, table, submission_id, query, job_config) -> None:
    """Deterministic job IDs also deduplicate overlapping retries of an uncertain insert.

    Confirmed failed jobs get a new attempt number; successful/in-flight jobs are reused.
    MERGE still protects ordinary retries after BigQuery's job metadata expires.
    """
    client = client_for(config)
    identity = hashlib.sha256(f"{table}\0{submission_id}".encode()).hexdigest()
    for attempt in range(100):
        job_id = f"dqm_missed_{identity}_{attempt}"
        try:
            job = client.get_job(job_id, location=config.location)
        except NotFound:
            try:
                job = client.query(query, job_config=job_config, job_id=job_id, job_retry=None)
            except Conflict:
                job = client.get_job(job_id, location=config.location)
        if job.state == "DONE" and job.error_result:
            continue
        job.result()
        return
    raise RuntimeError("Too many failed submissions for this report; resolve the warehouse error.")
