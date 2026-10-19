from __future__ import annotations

import getpass
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from . import browse
from .adapters import LazyModule, require_adapter
from .config import AppConfig
from .errors import AppError, SearchTooCostly, SnapshotUnavailable, WarehouseBusy, classify
from .limits import CacheLimitExceeded, check_cache_size, issue_json
from .relations import lock_key
from .store import EDITABLE_FIELDS, Patch

psycopg2 = LazyModule("psycopg2", "postgres")
pg_extras = LazyModule("psycopg2.extras", "postgres")
bigquery = LazyModule("google.cloud.bigquery", "bigquery")
google_errors = LazyModule("google.api_core.exceptions", "bigquery")
user_credentials = LazyModule("google.oauth2.credentials", "bigquery")
service_account = LazyModule("google.oauth2.service_account", "bigquery")

BIGQUERY_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
UNREADABLE_CREDENTIALS = "The BigQuery credentials file can't be read as a JSON credentials file."
UNSUPPORTED_CREDENTIALS = (
    "The BigQuery credentials file must be a service-account key or authorized-user (gcloud) "
    "credentials. For other credential types, use Application Default Credentials without a "
    "credentials file override."
)
INCOMPLETE_CREDENTIALS = "The BigQuery credentials file is missing required fields for its type."


def override_credentials(path: Path):
    """Validated credentials for an explicit credentials-file override.

    Only service-account keys and authorized-user (gcloud) files are accepted; generic loading
    could accept credential configurations that run commands or call arbitrary endpoints. The file
    is parsed once per version and the credentials are built from that parsed object. Errors are
    fixed messages: they never contain file contents or loader exception text.
    """
    try:
        stat = path.stat()
    except OSError:
        raise AppError(UNREADABLE_CREDENTIALS) from None
    return _override_credentials(str(path.resolve()), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=8)
def _override_credentials(path: str, mtime_ns: int, size: int):
    try:
        info = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise AppError(UNREADABLE_CREDENTIALS) from None
    kind = info.get("type") if isinstance(info, dict) else None
    if kind == "service_account":
        factory = service_account.Credentials.from_service_account_info
    elif kind == "authorized_user":
        factory = user_credentials.Credentials.from_authorized_user_info
    else:
        raise AppError(UNSUPPORTED_CREDENTIALS)
    try:
        return factory(info, scopes=BIGQUERY_SCOPES)
    except (ValueError, KeyError, TypeError):
        raise AppError(INCOMPLETE_CREDENTIALS) from None


logger = logging.getLogger(__name__)


def resolve_dbt_executable() -> str:
    directory = Path(sys.executable).parent.resolve()
    executable = shutil.which("dbt", path=str(directory))
    # Python 3.11's Windows which() inserts cwd before even an explicit path.
    if sys.platform == "win32" and (
        executable is None or Path(executable).resolve().parent != directory
    ):
        executable = next((found for suffix in (".exe", ".cmd", ".bat", ".com")
                           if (found := shutil.which(str(directory / ("dbt" + suffix))))), None)
    executable = executable or shutil.which("dbt")
    if executable is None:
        raise AppError("dbt could not be found. Install the selected dbt-dqm adapter extra "
                       "in this Python environment.")
    return str(Path(executable).resolve())


@lru_cache(maxsize=8)
def _dbt_version(executable: str) -> str:
    result = subprocess.run([executable, "--version"], check=True, capture_output=True, text=True,
                            encoding="utf-8", errors="replace",
                            env={**os.environ, "DBT_VERSION_CHECK": "false"})
    version = f"{result.stdout or ''}\n{result.stderr or ''}".strip()
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
        logger.setLevel(logging.INFO)
        logger.propagate = False
    logger.info("dbt executable: %s\ndbt --version:\n%s", executable, version)
    return version


def validate_dbt(config: AppConfig) -> None:
    """Use the active environment's dbt and preserve the caller's default resolution."""
    require_adapter(config.adapter_type)
    if config.credentials_file is not None:
        override_credentials(config.credentials_file)
    dbt_executable = resolve_dbt_executable()
    base = list(config.dbt_cli_args) if config.dbt_cli_args is not None else [
        "--project-dir", str(config.project_dir), "--profiles-dir", str(config.profiles_dir),
        "--target", config.target,
    ]
    environment = os.environ.copy()
    if config.credentials_file is not None:
        environment["GOOGLE_APPLICATION_CREDENTIALS"] = str(config.credentials_file.resolve())
    variables = yaml.safe_load(config.vars_yaml) or {}
    if variables:
        base += ["--vars", config.vars_yaml]
    version = ""
    try:
        version = _dbt_version(dbt_executable)
        for operation in ("debug", "parse"):
            subprocess.run([dbt_executable, operation, *base, *(["--vars", yaml.safe_dump({**variables, "dqm_enabled": True})] if operation == "parse" else [])], check=True, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", env=environment,
                           cwd=config.working_dir)
    except subprocess.CalledProcessError as error:
        failure = classify(error)
        failure.details += f"\ndbt --version:\n{version}"
        raise failure from None
    check_required_relations(config)


REQUIRED_APP_RELATIONS = (
    "dqm_install", "dqm_reconciliation_control", "dqm_issue_occurrences", "dqm_all_issues", "dqm_test_health",
    "dqm_area_health", "dqm_annotation_changes", "dqm_missed_issues",
)


def check_required_relations(config: AppConfig) -> None:
    missing = []
    if config.adapter_type == "bigquery":
        client = client_for(config)
        for name in (*REQUIRED_APP_RELATIONS, "dqm_app_change_staging"):
            relation = ".".join(_relation_parts(config, name))
            try:
                client.get_table(relation)
            except google_errors.NotFound:
                missing.append(relation)
    else:
        with _postgres_connection(config) as connection, connection.cursor() as cursor:
            for name in REQUIRED_APP_RELATIONS:
                relation = _table(config, name)
                cursor.execute("select to_regclass(%s)", (relation,))
                if cursor.fetchone()[0] is None:
                    missing.append(relation)
    if missing:
        raise AppError("DQM tracking relations are missing. Run `dbt-dqm run` "
                       "first, then Sync again.", "Missing relations:\n" + "\n".join(missing))

    state = _query_rows(config, f"select * from {_table(config, 'dqm_install')}")
    if len(state) != 1 or state[0]["marker"] != "dqm-full-v1" or not state[0].get("installation_id"):
        raise AppError("Incompatible DQM schema. Run `dbt-dqm run --full-refresh`, "
                       "then clear old reviewer workspaces and restart the app.")


def client_for(config: AppConfig) -> bigquery.Client:
    if config.adapter_type != "bigquery":
        raise ValueError("BigQuery client requested for a non-BigQuery profile")
    require_adapter("bigquery")
    if config.method == "service-account":
        if config.keyfile is None:
            raise ValueError("The dbt profile does not define keyfile")
        credentials = service_account.Credentials.from_service_account_file(config.keyfile)
        return bigquery.Client(
            project=config.project_id, credentials=credentials, location=config.location
        )
    if config.method == "oauth":
        if config.credentials_file is not None:
            credentials = override_credentials(config.credentials_file)
            return bigquery.Client(
                project=config.project_id, credentials=credentials, location=config.location
            )
        return bigquery.Client(project=config.project_id, location=config.location)
    raise ValueError(f"Unsupported BigQuery authentication method: {config.method}")


def _relation_parts(config: AppConfig, name: str) -> tuple[str | None, str, str]:
    if config.relations:
        found = next((row[1:] for row in config.relations if row[0] == name), None)
        if found is None:
            raise AppError(f'DQM relation is absent from the naming contract: {name}')
        return found
    manifest_path = (config.target_path or config.project_dir / 'target') / 'manifest.json'
    if manifest_path.exists() and 'model.dbt_dqm.dqm_reconcile' in manifest_nodes(config):
        from .relations import resolve_config
        return _relation_parts(resolve_config(config), name)
    if manifest_path.exists():
        node = manifest_nodes(config).get('model.dbt_dqm.' + name)
        if node:
            return node.get('database'), node['schema'], node.get('alias') or name
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
        connection.cursor(cursor_factory=pg_extras.RealDictCursor) as cursor,
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
                            cursor_factory=pg_extras.RealDictCursor) as cursor):
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
        f"(select max(installation_id) from {_table(config, 'dqm_install')}) as installation_id, "
        f"(select max(marker) from {_table(config, 'dqm_install')}) as installation_marker, "
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
    if before["setup_status"] != "ready" or before["installation_marker"] != "dqm-full-v1":
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
    if (after["generation"], after["installation_id"]) != (before["generation"], before["installation_id"]):
        raise SnapshotUnavailable(
            "The warehouse changed while syncing (a reconciliation finished). "
            "Showing cached data; sync again."
        )
    if after["setup_status"] != "ready" or after["installation_marker"] != "dqm-full-v1":
        raise SnapshotUnavailable("DQM setup changed while syncing. Cached data is kept.")
    if len(rows) != after["issue_count"]:
        raise SnapshotUnavailable(
            f"dqm_all_issues returned {len(rows)} issues but the tracking table holds "
            f"{after['issue_count']}. The public views may be out of date; rebuild "
            "package:dbt_dqm, then sync again. Showing cached data."
        )
    _validate_issue_rows(rows)
    for row in rows:
        row["_installation_id"] = after["installation_id"]
    return rows


SEARCH_TOO_COSTLY = (
    "This search would process about {estimate:.2f} GiB in BigQuery, above the {limit:g} GiB "
    "limit (--max-search-gib). Your current results are kept. Narrow the search with a "
    "lifecycle, created-date, status, owner or tag filter, or raise the limit."
)
GENERATION_CHANGED = (
    "The warehouse changed while reading (a reconciliation finished). Your current results are "
    "kept; restart from the first page."
)


def browse_population(config: AppConfig) -> str:
    """The issues warehouse browsing can show; fix it once per session so pages agree."""
    return _issue_predicate(config)


def _bigquery_parameters(params: dict[str, Any]) -> list:
    parameters = []
    for name, value in params.items():
        if isinstance(value, list):
            parameters.append(bigquery.ArrayQueryParameter(name, "STRING", value))
        elif isinstance(value, bool):
            parameters.append(bigquery.ScalarQueryParameter(name, "BOOL", value))
        elif isinstance(value, int):
            parameters.append(bigquery.ScalarQueryParameter(name, "INT64", value))
        elif isinstance(value, datetime):
            parameters.append(bigquery.ScalarQueryParameter(name, "TIMESTAMP", value))
        else:
            parameters.append(bigquery.ScalarQueryParameter(name, "STRING", value))
    return parameters


def _run_browse_query(config: AppConfig, query: browse.Query, *, searched: bool = False
                      ) -> list[dict[str, Any]]:
    """Run one parameterized browse query. A BigQuery search is estimated with a dry run first and
    then runs with maximum_bytes_billed, so the ceiling holds even if the estimate was low."""
    if config.adapter_type != "bigquery":
        with (
            _postgres_connection(config) as connection,
            connection.cursor(cursor_factory=pg_extras.RealDictCursor) as cursor,
        ):
            cursor.execute(query.sql, query.params)
            return [dict(row) for row in cursor.fetchall()]
    client = client_for(config)
    parameters = _bigquery_parameters(query.params)
    limit = None
    if searched:
        limit = config.max_search_bytes
        estimate = client.query(query.sql, job_config=bigquery.QueryJobConfig(
            query_parameters=parameters, dry_run=True, use_query_cache=False,
        )).total_bytes_processed or 0
        if estimate > limit:
            raise SearchTooCostly(SEARCH_TOO_COSTLY.format(
                estimate=estimate / 2**30, limit=config.max_search_gib))
    job_config = bigquery.QueryJobConfig(query_parameters=parameters)
    if limit is not None:
        job_config.maximum_bytes_billed = limit
    try:
        return [dict(row.items()) for row in client.query(query.sql, job_config=job_config).result()]
    except google_errors.GoogleAPICallError as error:
        if limit is not None and "bytesBilledLimitExceeded" in str(getattr(error, "errors", "")) + str(error):
            raise SearchTooCostly(SEARCH_TOO_COSTLY.format(
                estimate=limit / 2**30, limit=config.max_search_gib), str(error)) from error
        raise


STATE_COLUMNS = ("_generation", "_setup_status", "_installation_marker")
SETUP_NOT_READY = (
    "dbt-dqm setup or a migration is in progress, so warehouse data isn't ready yet. "
    "Try again shortly."
)


def _read_with_state(config: AppConfig, query: browse.Query, *, order: str | None = None,
                     searched: bool = False) -> tuple[list[dict[str, Any]], Any]:
    """Run one browse query in the same statement as a read of the control state.

    One statement reads one snapshot on both adapters, so the rows and the generation (and setup
    status) returned with them are consistent: no separate before/after checks are needed. The
    control row is left-joined so an empty result still carries the state.
    """
    control = _table(config, "dqm_reconciliation_control")
    state = (f"select max(generation) as _generation, max(setup_status) as _setup_status "
             f", (select max(installation_id) from {_table(config, 'dqm_install')}) as _installation_id "
             f", (select max(marker) from {_table(config, 'dqm_install')}) as _installation_marker "
             f"from {control}")
    sql = f"select s.*, q.* from ({state}) s left join ({query.sql}) q on true"
    if order:
        sql += f" order by {order}"
    rows = _run_browse_query(config, browse.Query(sql, query.params), searched=searched)
    if not rows or rows[0]["_setup_status"] != "ready" or rows[0]["_installation_marker"] != "dqm-full-v1":
        raise SnapshotUnavailable(SETUP_NOT_READY)
    generation = (rows[0]["_installation_id"], rows[0]["_generation"])
    return [{key: value for key, value in row.items() if key not in STATE_COLUMNS}
            for row in rows], generation


@dataclass
class Page:
    rows: list[dict[str, Any]]
    next_cursor: browse.Cursor | None
    generation: Any


PAGE_ORDER = "q._priority_rank, q.first_seen_at, q.occurrence_id"


def iter_export_rows(config, population, filters, expected_generation):
    """Stream a single read snapshot; exports always enforce the BigQuery byte ceiling."""
    query = browse.export_query(config.adapter_type, _table(config, "dqm_all_issues"),
                                population, filters)
    control = _table(config, "dqm_reconciliation_control")
    state = (f"select max(generation) as _generation,max(setup_status) as _setup_status,"
             f"(select max(installation_id) from {_table(config, 'dqm_install')}) as _installation_id,"
             f"(select max(marker) from {_table(config, 'dqm_install')}) as _installation_marker "
             f"from {control}")
    sql = f"select s.*,q.* from ({state}) s left join ({query.sql}) q on true"
    sql += f" order by {browse._priority_rank_sql()},q.first_seen_at,q.occurrence_id"

    def checked(rows):
        for row in rows:
            row = dict(row.items())
            if row["_setup_status"] != "ready" or row["_installation_marker"] != "dqm-full-v1":
                raise SnapshotUnavailable(SETUP_NOT_READY)
            generation = (row["_installation_id"], row["_generation"])
            if generation != expected_generation:
                raise SnapshotUnavailable("The warehouse changed. Sync or restart browsing, then export again.")
            if row.get("occurrence_id") is not None:
                _validate_issue_rows([row])
                yield row

    if config.adapter_type == "bigquery":
        client = client_for(config)
        parameters = _bigquery_parameters(query.params)
        estimate = client.query(sql, job_config=bigquery.QueryJobConfig(
            query_parameters=parameters, dry_run=True, use_query_cache=False)).total_bytes_processed or 0
        if estimate > config.max_search_bytes:
            raise SearchTooCostly("This export exceeds the BigQuery query byte limit. "
                                 "Narrow the filters or increase --max-search-gib.")
        try:
            rows = client.query(sql, job_config=bigquery.QueryJobConfig(
                query_parameters=parameters, maximum_bytes_billed=config.max_search_bytes)).result(page_size=500)
            yield from checked(rows)
        except google_errors.GoogleAPICallError as error:
            if "bytesBilledLimitExceeded" in str(getattr(error, "errors", "")) + str(error):
                raise SearchTooCostly("This export exceeds the BigQuery query byte limit. "
                                     "Narrow the filters or increase --max-search-gib.") from error
            raise
    else:
        with _postgres_connection(config) as connection:
            connection.set_session(readonly=True, isolation_level="REPEATABLE READ")
            with connection.cursor(name="dqm_export", cursor_factory=pg_extras.RealDictCursor) as cursor:
                cursor.itersize = 500
                cursor.execute(sql, query.params)
                yield from checked(cursor)
    # The completed query was coherent; reject if the installation/generation changed during delivery.
    _, latest = _read_with_state(config, browse.Query("select 1 as export_check"))
    if latest != expected_generation:
        raise SnapshotUnavailable("The warehouse changed during export. Sync and export again.")


def browse_page(config: AppConfig, population: str, filters: browse.Filters,
                cursor: browse.Cursor | None, page_size: int) -> Page:
    """One verified page in snapshot sort order, plus the cursor for the next one.

    Postgres reads the page in one statement. BigQuery reads in two phases so payload columns
    are scanned for one page only: the key columns choose the page, then that page's full rows
    are read by id; both statements carry the generation, and a page whose two reads disagree is
    read again once, then reported.
    """
    relation = _table(config, "dqm_all_issues")
    searched = browse.normalized_search(filters.search) is not None
    adapter = config.adapter_type
    for _attempt in range(2):
        if adapter != "bigquery":
            rows, generation = _read_with_state(config, browse.page_query(
                adapter, relation, population, filters, cursor, page_size),
                order=PAGE_ORDER, searched=searched)
            keys = [row for row in rows if row.get("occurrence_id") is not None]
            page = keys[:page_size]
            break
        found, generation = _read_with_state(config, browse.page_query(
            adapter, relation, population, filters, cursor, page_size,
            columns=", ".join(browse.KEY_COLUMNS)), order=PAGE_ORDER, searched=searched)
        keys = [row for row in found if row.get("occurrence_id") is not None]
        ids = [str(row["occurrence_id"]) for row in keys[:page_size]]
        if not ids:
            page = []
            break
        full, full_generation = _read_with_state(
            config, browse.rows_by_id_query(adapter, relation, ids))
        by_id = {str(row["occurrence_id"]): row for row in full
                 if row.get("occurrence_id") is not None}
        if full_generation == generation and set(by_id) == set(ids):
            page = [by_id[occurrence_id] for occurrence_id in ids]
            break
    else:
        raise SnapshotUnavailable(GENERATION_CHANGED)
    next_cursor = browse.cursor_from_row(keys[page_size - 1]) if len(keys) > page_size else None
    for row in page:
        row.pop("_priority_rank", None)
        row.pop("_status_rank", None)
    _validate_issue_rows(page)
    return Page(page, next_cursor, generation)


def browse_summary(config: AppConfig, population: str, filters: browse.Filters) -> dict[str, Any]:
    """The header metrics over the whole population (as in snapshot mode) and the filtered count,
    in one statement."""
    relation = _table(config, "dqm_all_issues")
    adapter = config.adapter_type
    metrics = browse.metrics_query(adapter, relation, population)
    count = browse.count_query(adapter, relation, population, filters)
    combined = browse.Query(f"select * from ({metrics.sql}) m cross join ({count.sql}) c",
                            {**metrics.params, **count.params})
    rows, generation = _read_with_state(
        config, combined, searched=browse.normalized_search(filters.search) is not None)
    return {**{key: int(value or 0) for key, value in rows[0].items() if key != "_installation_id"}, "generation": generation}


def browse_options(config: AppConfig, population: str) -> dict[str, Any]:
    """Filter choices for warehouse browsing, read with the generation in one statement."""
    relation = _table(config, "dqm_all_issues")
    rows, generation = _read_with_state(
        config, browse.options_query(config.adapter_type, relation, population))
    row = rows[0]
    return {
        "statuses": [str(value) for value in row["statuses"] or []],
        "owners": [str(value) for value in row["owners"] or []],
        "tags": [str(value) for value in row["tags"] or []],
        "first_seen_min": row["first_seen_min"],
        "first_seen_max": row["first_seen_max"],
        "generation": generation,
    }


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
    current_installation = installation_id(config)
    if any(patch.installation_id != current_installation for patch in patches):
        raise AppError("Stale DQM installation. Clear the old workspace after full refresh.")
    staging_id = ".".join(_relation_parts(config, "dqm_app_change_staging"))
    try:
        table = client.get_table(staging_id)
    except google_errors.NotFound as error:
        raise AppError("App staging is missing. Build package:dbt_dqm before applying.") from error
    if not {"upload_id", "staged_at", "installation_id"} <= {field.name for field in table.schema}:
        raise AppError("App staging requires the new DQM schema. Run dbt-dqm run --full-refresh, "
                       "then clear old workspaces and restart reviewer apps.")
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
            ("base_annotation_version", "INT64"), ("installation_id", "STRING"))],
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
      create temp table attempt default collate '' as
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
      assert (select marker from {_table(config, 'dqm_install')})='dqm-full-v1'
        as 'DQM reset is in progress; stop reviewer apps until it completes.';
      assert not exists(select 1 from attempt where installation_id is null or
        installation_id != (select installation_id from { _table(config, 'dqm_install') }))
        as 'Stale DQM installation. Clear the old workspace after full refresh.';
      set audited_count=(select count(*) from {audit} where batch_id=@batch_id);
      if audited_count>0 then
        assert audited_count=@patch_count and not exists (
          select 1 from attempt s left join {audit} a
            on a.batch_id=@batch_id and a.occurrence_id=s.occurrence_id and a.field_name=s.field_name
          where a.occurrence_id is null or collate(a.new_value, '') is distinct from collate(s.new_value, '')
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
    require_adapter("postgres")
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
        connection.cursor(cursor_factory=pg_extras.RealDictCursor) as cursor,
    ):
        cursor.execute("select pg_advisory_xact_lock(hashtext(%s))", (lock_key(config),))
        cursor.execute(f"select installation_id from {_table(config, 'dqm_install')}")
        installation = cursor.fetchone()["installation_id"]
        if any(patch.installation_id != installation for patch in patches):
            raise AppError("Stale DQM installation. Clear the old workspace after full refresh.")
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


def worker_error(error: Exception) -> AppError:
    """Keep only portable strings and known app exception types across process boundaries."""
    failure = classify(error)
    kind = type(failure) if type(failure) in (
        AppError, SnapshotUnavailable, SearchTooCostly, WarehouseBusy, CacheLimitExceeded,
    ) else AppError
    return kind(str(failure.message), str(failure.details))


def run_worker(worker, *args):
    try:
        return worker(*args)
    except Exception as error:  # noqa: BLE001 - third-party exceptions cannot cross the pool.
        raise worker_error(error) from None


def installation_id(config: AppConfig) -> str:
    return _query_rows(config, f"select installation_id from {_table(config, 'dqm_install')}")[0]["installation_id"]


def _refresh_snapshot(config: AppConfig, browsing=False) -> dict[str, Any]:
    epoch = installation_id(config)
    snapshot = {"health": fetch_health(config)} if browsing else {
        "issues": fetch_issues(config), "health": fetch_health(config)}
    if installation_id(config) != epoch:
        raise SnapshotUnavailable("The DQM installation changed while syncing. Clear the old workspace after full refresh.")
    return {**snapshot, "installation_id": epoch}


def sync_worker(config: AppConfig) -> dict[str, Any]:
    validate_dbt(config)
    return _refresh_snapshot(config)


def browse_refresh_worker(config: AppConfig) -> dict[str, Any]:
    """Warehouse mode's sync: issue pages are read on demand, so only Health is refreshed."""
    validate_dbt(config)
    return _refresh_snapshot(config, browsing=True)


def apply_worker(
    config: AppConfig, patches: list[Patch], browsing: bool = False
) -> tuple[str, dict[str, Any] | None, AppError | None]:
    """Apply patches, then refresh. A failed refresh doesn't undo or hide a committed apply.

    In warehouse mode the refresh is Health only; the app re-reads the page on screen.
    """
    validate_dbt(config)
    batch_id = apply_patches(config, patches)
    try:
        return batch_id, _refresh_snapshot(config, browsing), None
    except Exception as error:  # noqa: BLE001 - the apply already committed; report the refresh.
        return batch_id, None, worker_error(error)


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
    # Maintenance health arrives with migration 0005. A reviewer app that is newer than the
    # deployed package reports it as unavailable instead of failing the whole sync.
    try:
        health["maintenance"] = _query_rows(
            config, f"select * from {_table(config, 'dqm_maintenance_health')} order by step"
        )
    except Exception as error:
        modules = {cls.__module__ for cls in type(error).__mro__}
        missing_table = (
            "google.api_core.exceptions" in modules and isinstance(error, google_errors.NotFound)
        ) or (
            any(module.startswith("psycopg2") for module in modules)
            and isinstance(error, psycopg2.errors.UndefinedTable)
        )
        if not missing_table:
            raise
        health["maintenance"] = None
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
        except google_errors.NotFound:
            try:
                job = client.query(query, job_config=job_config, job_id=job_id, job_retry=None)
            except google_errors.Conflict:
                job = client.get_job(job_id, location=config.location)
        if job.state == "DONE" and job.error_result:
            continue
        job.result()
        return
    raise RuntimeError("Too many failed submissions for this report; resolve the warehouse error.")
