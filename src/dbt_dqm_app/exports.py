"""Read-only, bounded exports of saved issues; never overlay local annotation patches."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import sys
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .browse import SEARCH_COLUMNS, Filters
from .display import calendar_date, is_missing, tag_list
from .errors import AppError
from .limits import issue_json

MAX_EXPORT_ISSUES = 50_000
MAX_EXPORT_BYTES = 128 * 2**20
ISSUE_COLUMNS = (
    "occurrence_id", "test_unique_id", "test_name", "unique_id", "occurrence_number",
    "record_values_json", "failure_row_count", "test_tags", "source_unique_id", "source_relation",
    "test_severity", "first_seen_at", "last_seen_at", "archived_at", "record_status", "close_reason",
    "first_seen_invocation", "last_seen_invocation", "last_evaluated_invocation", "workflow_status",
    "test_status", "call_to_action", "ticket_url", "notes", "dqm_owner", "annotation_updated_at",
    "annotation_version", "granularity_signature", "identity_scheme_signature", "test_priority",
    "test_criticality", "review_verdict", "workflow_status_at_close", "has_previous_occurrence",
    "is_recurrence", "is_potential_regression", "previous_occurrence_id", "previous_archived_at",
    "previous_close_reason", "previous_workflow_status_at_close", "previous_review_verdict",
    "previous_dqm_owner", "previous_ticket_url", "previous_notes",
)


LEADING_COLUMNS = ('test_name', 'dqm_owner', 'record_values_json', 'record_status',
                   'workflow_status', 'review_verdict', 'test_status', 'first_seen_at',
                   'last_seen_at', 'annotation_updated_at', 'call_to_action', 'ticket_url', 'notes')
TIMESTAMP_COLUMNS = ('first_seen_at', 'last_seen_at', 'annotation_updated_at',
                     'archived_at', 'previous_archived_at')


def _timestamp_zone(value):
    try:
        parsed = datetime.fromisoformat(str(value))
        offset = parsed.utcoffset()
        return 'timezone unspecified' if offset is None else 'UTC' if offset == timedelta(0) else 'offset included'
    except (ValueError, TypeError):
        return 'timezone unspecified'


def _title(key, zones):
    if key not in TIMESTAMP_COLUMNS:
        return key
    labels = zones.get(key) or {'UTC'}
    return key + ' (' + ' / '.join(sorted(labels)) + ')'


def _private(path, mode):
    # Windows uses inherited ACLs under the private workspace directory; chmod only changes
    # the read-only attribute and can conflict with an open SQLite handle.
    if sys.platform != "win32":
        path.chmod(mode)


def matches_filters(row, filters: Filters) -> bool:
    """Same saved-value predicates as warehouse browsing, without a pending overlay."""
    status = str(row.get("workflow_status") or "NEW").upper()
    owner = row.get("dqm_owner")
    owner = "Unassigned" if is_missing(owner) else str(owner).strip() or "Unassigned"
    priority = row.get("test_priority")
    priority = "unset" if is_missing(priority) else priority
    if (filters.lifecycle and row.get("record_status") not in filters.lifecycle or
            filters.statuses and status not in filters.statuses or
            filters.owners and owner not in filters.owners or
            filters.priorities and priority not in filters.priorities or
            filters.tags and not set(tag_list(row.get("test_tags"))) & set(filters.tags) or
            filters.recurrences_only and (is_missing(row.get("is_recurrence")) or not row.get("is_recurrence"))):
        return False
    created = calendar_date(row.get("first_seen_at"))
    if (filters.created_from and (created is None or created < filters.created_from) or
            filters.created_to and (created is None or created > filters.created_to)):
        return False
    search = (filters.search or "").strip().casefold()
    return not search or search in " ".join(str(row.get(key) or "") for key in SEARCH_COLUMNS).casefold()


def snapshot_rows(path, filters, expected_sync):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("begin")
        sync = connection.execute("select value from metadata where key='last_sync'").fetchone()
        if (sync[0] if sync else None) != expected_sync:
            raise AppError("Saved issues changed. Prepare the export again.")
        for (payload,) in connection.execute("select payload_json from snapshot order by occurrence_id"):
            row = json.loads(payload)
            if matches_filters(row, filters):
                yield row


@dataclass(frozen=True)
class ExportResult:
    path: str
    rows: int
    format: str


def _record(row):
    """Expand top-level fields without rounding JSON numbers or rewriting nested JSON."""
    try:
        raw = row["record_values_json"]
        if not isinstance(raw, str):
            raise TypeError("record values must be JSON text")
        def reject_constant(value):
            raise ValueError("nonstandard JSON constant")

        decoder = json.JSONDecoder(parse_int=str, parse_float=str, parse_constant=reject_constant)
        whitespace = re.compile(r"[ \t\r\n]*")

        def skip(position):
            return whitespace.match(raw, position).end()

        position = skip(0)
        if raw[position:position + 1] != "{":
            raise ValueError("record values must be an object")
        position = skip(position + 1)
        result = {}
        while raw[position:position + 1] != "}":
            if raw[position:position + 1] != '"':
                raise ValueError("record keys must be strings")
            key, position = decoder.raw_decode(raw, position)
            if not isinstance(key, str) or key in result:
                raise ValueError("record keys must be unique strings")
            position = skip(position)
            if raw[position:position + 1] != ":":
                raise ValueError("missing record separator")
            start = skip(position + 1)
            value, position = decoder.raw_decode(raw, start)
            result[key] = raw[start:position] if isinstance(value, (dict, list)) else value
            position = skip(position)
            if raw[position:position + 1] == "}":
                break
            if raw[position:position + 1] != ",":
                raise ValueError("missing record separator")
            position = skip(position + 1)
            if raw[position:position + 1] == "}":
                raise ValueError("trailing record comma")
        if skip(position + 1) != len(raw):
            raise ValueError("unexpected record content")
        return result
    except (ValueError, TypeError, KeyError) as error:
        raise AppError(f"Issue {row.get('occurrence_id')}: record JSON cannot be expanded "
                       "completely in Excel. Use CSV instead.") from error


def _text(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _csv_value(value):
    text = _text(value)
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def _check_file(path):
    if path.stat().st_size > MAX_EXPORT_BYTES:
        raise AppError("The export exceeds 128 MiB. Narrow the filters and prepare it again.")


def _sheet_name(test_name, test_id):
    name = re.sub(r"[\[\]:*?/\\\x00-\x1f]", "_", str(test_name)).strip("'") or "Test"
    return name[:22] + "_" + hashlib.sha256(test_id.encode()).hexdigest()[:8]


def write_export(rows, directory, format, granularity=None):
    if format not in ("csv", "xlsx"):
        raise ValueError("Export format must be csv or xlsx")
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _private(directory, 0o700)
    spool = directory / "rows.sqlite"
    destination = directory / ("issues." + format)
    columns = set(ISSUE_COLUMNS)
    tests = {}
    zones = {}
    granularity = granularity or {}
    count = size = 0
    try:
        with closing(sqlite3.connect(spool)) as connection:
            _private(spool, 0o600)
            connection.execute("create table rows (position integer primary key,test_id text,payload text)")
            for row in rows:
                public = {key: value for key, value in row.items() if not key.startswith("_")}
                payload = issue_json(public)
                count += 1
                size += len(payload.encode("utf-8"))
                if count > MAX_EXPORT_ISSUES or size > MAX_EXPORT_BYTES:
                    raise AppError("The export exceeds 50,000 issues or 128 MiB of source data. "
                                   "Narrow the filters and prepare it again.")
                record = _record(public) if format == 'xlsx' else {}
                test_id = str(public.get("test_unique_id") or public.get("test_name") or "Unnamed test")
                test = tests.setdefault(test_id, {"name": public.get("test_name") or "Unnamed test", "keys": {}})
                for key in record:
                    test['keys'].setdefault(key, None)
                for key in TIMESTAMP_COLUMNS:
                    if public.get(key) is not None and str(public[key]).strip():
                        zones.setdefault(key, set()).add(_timestamp_zone(public[key]))
                columns.update(public)
                connection.execute("insert into rows values (?,?,?)", (count, test_id, payload))
            connection.commit()
            connection.execute("create index rows_by_test on rows(test_id,position)")
            headers = [*LEADING_COLUMNS, *[key for key in ISSUE_COLUMNS if key not in LEADING_COLUMNS],
                       *sorted(columns - set(ISSUE_COLUMNS))]
            if format == "csv":
                with destination.open("w", encoding="utf-8-sig", newline="") as output:
                    _private(destination, 0o600)
                    writer = csv.writer(output)
                    writer.writerow([_title(key, zones) for key in headers])
                    for (payload,) in connection.execute("select payload from rows order by position"):
                        row = json.loads(payload)
                        writer.writerow([_csv_value(row.get(key)) for key in headers])
                        output.flush()
                        _check_file(destination)
            else:
                _write_excel(connection, destination, headers, tests, granularity, zones)
            _check_file(destination)
        return ExportResult(str(destination), count, format)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        spool.unlink(missing_ok=True)
        close = getattr(rows, "close", None)
        if close:
            close()


def _write_excel(connection, destination, headers, tests, granularity, zones):
    import xlsxwriter

    used = set()
    with tempfile.TemporaryDirectory(prefix="xlsx-", dir=destination.parent) as temporary, \
            xlsxwriter.Workbook(destination, {"constant_memory": True,
            "tmpdir": temporary,
            "strings_to_formulas": False, "strings_to_urls": False}) as workbook:
        header_style = workbook.add_format({"bold": True})
        groups = sorted(tests.items()) or [("", {"name": "No issues", "keys": {}})]
        for test_id, test in groups:
            name = _sheet_name(test["name"], test_id) if test_id else "No issues"
            counter = 0
            while name.casefold() in used:
                counter += 1
                name = _sheet_name(test["name"], test_id + str(counter))
            used.add(name.casefold())
            sheet = workbook.add_worksheet(name)
            keys = list(test['keys'])
            grain = []
            for configured in granularity.get(test_id, []):
                found = next((key for key in keys if key.casefold() == str(configured).strip().casefold()), None)
                if found is not None and found not in grain:
                    grain.append(found)
            keys = grain + [key for key in keys if key not in grain]
            fields = [*headers[:2], *['record.' + key for key in keys], *headers[3:]]
            titles = [_title(key, zones) if not key.startswith('record.') else key for key in fields]
            if len(titles) > 16_384:
                raise AppError("This test has too many columns for Excel. Use CSV instead.")
            for column, title in enumerate(titles):
                _excel_text(sheet, 0, column, title, header_style)
            position = 0
            for (payload,) in connection.execute("select payload from rows where test_id=? order by position", (test_id,)):
                position += 1
                row = json.loads(payload)
                record = _record(row)
                values = [row.get(key) for key in headers[:2]] + [record.get(key) for key in keys] + [row.get(key) for key in headers[3:]]
                for column, value in enumerate(values):
                    _excel_text(sheet, position, column, _text(value))
            sheet.freeze_panes(1, 0)
            sheet.autofilter(0, 0, max(1, position), len(titles) - 1)
            sheet.set_column(0, len(titles) - 1, 22)
    _private(destination, 0o600)


def _excel_text(sheet, row, column, text, style=None):
    if len(text) > 32_767 or text.count("\n") > 253 or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text):
        raise AppError("A value cannot be represented fully in Excel. Use CSV instead.")
    if sheet.write_string(row, column, text, style) != 0:
        raise AppError("Excel could not represent the complete value. Use CSV instead.")


def prepare_export(config, path, filters, directory, format, population=None, expected=None):
    from .warehouse import iter_export_rows

    artifact = (config.target_path or config.project_dir / 'target') / 'manifest.json'
    if not config.relations and artifact.exists():
        from .relations import resolve_config
        config = resolve_config(config)
    rows = (snapshot_rows(path, filters, expected) if population is None else
            iter_export_rows(config, population, filters, expected))
    from .warehouse import manifest_nodes
    artifact = (config.target_path or config.project_dir / 'target') / 'manifest.json'
    nodes = manifest_nodes(config) if artifact.exists() else {}
    granularity = dict(config.test_granularity) or {key: (node.get('config', {}).get('meta', {}).get('dbt_dqm') or {}).get('granularity', [])
                   for key, node in nodes.items() if node.get('resource_type') == 'test'}
    return write_export(rows, directory, format, granularity)
