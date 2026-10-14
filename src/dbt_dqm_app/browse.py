"""Warehouse-side issue browsing: filters, ordering and keyset pages as parameterized SQL.

This module only builds SQL and parameters; `warehouse.py` runs them. Every value a reviewer
supplies (filter choices, dates, search text, cursors) is passed as a query parameter and never
interpolated into SQL. Semantics match the snapshot mode's pandas filters in `streamlit_app`:

- lifecycle, priority (missing = "unset"), tags (trimmed JSON array elements), recurrences only;
- workflow status (missing = "NEW", compared upper-case);
- owner (blank or missing = "Unassigned");
- created date: an inclusive range of local calendar dates, passed as UTC bounds;
- order: priority rank, created time, occurrence id. Edits never affect sort keys.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

SEARCH_MIN, SEARCH_MAX = 3, 200
PAGE_SIZES = (25, 50, 100, 200)
# BigQuery bills the referenced columns of every row whether a read returns 25 rows or 2,000, so
# the app reads BigQuery pages in chunks this size (a multiple of every page size, and the page
# cache bound) and pages through each chunk locally.
CHUNK_ISSUES = 2_000
STATUS_ORDER = (
    "NEW", "BLOCKED", "IN_PROGRESS", "TRIAGED", "RESOLVED", "ACCEPTED_RISK", "FALSE_POSITIVE",
)
PRIORITY_ORDER = ("critical", "high", "medium", "low")
SEARCH_COLUMNS = (
    "test_name", "unique_id", "record_values_json", "workflow_status", "dqm_owner",
    "call_to_action", "notes", "ticket_url", "test_priority", "test_tags",
)
# Phase 1 of a BigQuery page reads only these (plus filter columns) to choose the page's ids.
KEY_COLUMNS = ("occurrence_id", "first_seen_at", "test_priority")


class InvalidSearch(ValueError):
    """The search text is too short or too long."""


@dataclass(frozen=True)
class Filters:
    lifecycle: tuple[str, ...] = ("Active",)
    statuses: tuple[str, ...] = ()
    owners: tuple[str, ...] = ()
    created_from: date | None = None
    created_to: date | None = None
    priorities: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    recurrences_only: bool = False
    search: str | None = None


@dataclass(frozen=True)
class Cursor:
    """The last row of the previous page, in sort-key order."""

    priority_rank: int
    first_seen_at: datetime
    occurrence_id: str


@dataclass
class Query:
    sql: str
    params: dict[str, Any] = field(default_factory=dict)


def normalized_search(text: str | None) -> str | None:
    if text is None or not text.strip():
        return None
    value = text.strip()
    if not SEARCH_MIN <= len(value) <= SEARCH_MAX:
        raise InvalidSearch(f"Search needs {SEARCH_MIN} to {SEARCH_MAX} characters.")
    return value


def created_bounds(
    start: date | None, end: date | None, tz=None
) -> tuple[datetime | None, datetime | None]:
    """UTC bounds for an inclusive range of local calendar dates: [start 00:00, end+1 00:00)."""
    tz = tz or datetime.now().astimezone().tzinfo
    lower = datetime.combine(start, time.min, tz).astimezone(UTC) if start else None
    upper = datetime.combine(end + timedelta(days=1), time.min, tz).astimezone(UTC) if end else None
    return lower, upper


class _Builder:
    """Collects predicates and parameters in one adapter's parameter style."""

    def __init__(self, adapter: str):
        if adapter not in ("postgres", "bigquery"):
            raise ValueError(f"Unsupported adapter: {adapter}")
        self.adapter = adapter
        self.params: dict[str, Any] = {}
        self.where: list[str] = []

    def param(self, name: str, value: Any) -> str:
        self.params[name] = value
        return f"@{name}" if self.adapter == "bigquery" else f"%({name})s"

    def in_list(self, expression: str, name: str, values: tuple[str, ...]) -> str:
        placeholder = self.param(name, list(values))
        if self.adapter == "bigquery":
            return f"{expression} in unnest({placeholder})"
        return f"{expression} = any({placeholder})"


def _priority_rank_sql() -> str:
    cases = " ".join(f"when '{value}' then {rank}" for rank, value in enumerate(PRIORITY_ORDER))
    return f"(case lower(coalesce(test_priority, '')) {cases} else {len(PRIORITY_ORDER)} end)"


def _status_sql() -> str:
    return "upper(coalesce(workflow_status, 'NEW'))"


def _owner_sql() -> str:
    return "coalesce(nullif(trim(dqm_owner), ''), 'Unassigned')"


def _tags_exist_sql(builder: _Builder, tags: tuple[str, ...]) -> str:
    placeholder = builder.param("tags", list(tags))
    if builder.adapter == "bigquery":
        # json_value_array returns null for malformed JSON, so such rows simply don't match.
        return (
            "exists(select 1 from unnest(json_value_array(test_tags)) tag "
            f"where trim(tag) in unnest({placeholder}))"
        )
    # Capture writes test_tags as a compact JSON array; anything else counts as no tags.
    elements = (
        "case when jsonb_typeof(test_tags::jsonb) = 'array' then test_tags::jsonb "
        "else '[]'::jsonb end"
    )
    return (
        f"exists(select 1 from jsonb_array_elements_text({elements}) tag "
        f"where trim(tag) = any({placeholder}))"
    )


def _search_sql(builder: _Builder, text: str) -> str:
    """Case-insensitive substring match over SEARCH_COLUMNS, with no wildcard interpretation."""
    text_type = "string" if builder.adapter == "bigquery" else "text"
    parts = [f"coalesce(cast({column} as {text_type}), '')" for column in SEARCH_COLUMNS]
    if builder.adapter == "bigquery":
        placeholder = builder.param("search", text)
        joined = ", ' ', ".join(parts)
        return f"contains_substr(concat({joined}), {placeholder})"
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    placeholder = builder.param("search", f"%{escaped}%")
    joined = ", ".join(parts)
    return f"concat_ws(' ', {joined}) ilike {placeholder} escape '\\'"


def _apply_filters(builder: _Builder, population: str, filters: Filters) -> None:
    builder.where.append(population)
    if filters.lifecycle:
        builder.where.append(builder.in_list("record_status", "lifecycle", filters.lifecycle))
    if filters.statuses:
        builder.where.append(builder.in_list(_status_sql(), "statuses", filters.statuses))
    if filters.owners:
        builder.where.append(builder.in_list(_owner_sql(), "owners", filters.owners))
    if filters.priorities:
        builder.where.append(builder.in_list("coalesce(test_priority, 'unset')", "priorities", filters.priorities))
    if filters.tags:
        builder.where.append(_tags_exist_sql(builder, filters.tags))
    if filters.recurrences_only:
        builder.where.append("coalesce(is_recurrence, false)")
    lower, upper = created_bounds(filters.created_from, filters.created_to)
    if lower is not None:
        builder.where.append(f"first_seen_at >= {builder.param('created_from', _naive_if_postgres(builder, lower))}")
    if upper is not None:
        builder.where.append(f"first_seen_at < {builder.param('created_to', _naive_if_postgres(builder, upper))}")
    search = normalized_search(filters.search)
    if search:
        builder.where.append(_search_sql(builder, search))


def _naive_if_postgres(builder: _Builder, moment: datetime) -> datetime:
    """Postgres stores issue timestamps as UTC `timestamp without time zone`."""
    return moment.astimezone(UTC).replace(tzinfo=None) if builder.adapter == "postgres" else moment


def _order_and_keyset(builder: _Builder, cursor: Cursor | None) -> tuple[str, str | None]:
    keys = (_priority_rank_sql(), "first_seen_at", "occurrence_id")
    order = ", ".join(keys)
    if cursor is None:
        return order, None
    values = (
        builder.param("after_priority", cursor.priority_rank),
        builder.param("after_seen", _naive_if_postgres(builder, cursor.first_seen_at)),
        builder.param("after_id", cursor.occurrence_id),
    )
    if builder.adapter == "postgres":
        return order, f"({', '.join(keys)}) > ({', '.join(values)})"
    terms = []
    for index in range(len(keys)):
        equal = [f"{keys[i]} = {values[i]}" for i in range(index)]
        terms.append("(" + " and ".join([*equal, f"{keys[index]} > {values[index]}"]) + ")")
    return order, "(" + " or ".join(terms) + ")"


def page_query(adapter: str, relation: str, population: str, filters: Filters,
               cursor: Cursor | None, page_size: int, columns: str = "*") -> Query:
    """One page in sort order. On BigQuery call it with KEY_COLUMNS first (phase 1), then read the
    full rows for the returned ids with `rows_by_id_query` (phase 2)."""
    if page_size not in (*PAGE_SIZES, CHUNK_ISSUES):
        raise ValueError(f"Page size must be one of {PAGE_SIZES} or {CHUNK_ISSUES}")
    builder = _Builder(adapter)
    _apply_filters(builder, population, filters)
    order, keyset = _order_and_keyset(builder, cursor)
    if keyset:
        builder.where.append(keyset)
    ranks = f"{_priority_rank_sql()} as _priority_rank"
    limit = builder.param("page_limit", page_size + 1)  # one extra row says whether a next page exists
    sql = (f"select {columns}, {ranks} from {relation} where {' and '.join(builder.where)} "
           f"order by {order} limit {limit}")
    return Query(sql, builder.params)


def rows_by_id_query(adapter: str, relation: str, ids: list[str]) -> Query:
    builder = _Builder(adapter)
    condition = builder.in_list("occurrence_id", "ids", tuple(ids))
    ranks = f"{_priority_rank_sql()} as _priority_rank"
    return Query(f"select *, {ranks} from {relation} where {condition}", builder.params)


def count_query(adapter: str, relation: str, population: str, filters: Filters) -> Query:
    """How many issues match the filters ("Showing N of M")."""
    builder = _Builder(adapter)
    _apply_filters(builder, population, filters)
    return Query(f"select count(*) as matching from {relation} where {' and '.join(builder.where)}",
                 builder.params)


def export_query(adapter: str, relation: str, population: str, filters: Filters) -> Query:
    """One bounded export of the whole filtered population, without a page cursor."""
    builder = _Builder(adapter)
    _apply_filters(builder, population, filters)
    order, _ = _order_and_keyset(builder, None)
    return Query(f"select * from {relation} where {' and '.join(builder.where)} "
                 f"order by {order} limit 50001", builder.params)


def metrics_query(adapter: str, relation: str, population: str) -> Query:
    """The header metrics over the whole population, as in snapshot mode (not the filtered list)."""
    active = "record_status = 'Active'"
    if adapter == "bigquery":
        expressions = [f"countif({active}) as active_count",
                       f"countif({active} and {_status_sql()} = 'NEW') as new_count",
                       f"countif({active} and {_status_sql()} = 'BLOCKED') as blocked_count",
                       f"countif({active} and coalesce(is_recurrence, false)) as recurring_count"]
    else:
        count = "count(*) filter"
        expressions = [f"{count} (where {active}) as active_count",
                       f"{count} (where {active} and {_status_sql()} = 'NEW') as new_count",
                       f"{count} (where {active} and {_status_sql()} = 'BLOCKED') as blocked_count",
                       f"{count} (where {active} and coalesce(is_recurrence, false)) as recurring_count"]
    return Query(f"select {', '.join(expressions)} from {relation} where {population}")


def cursor_from_row(row: dict[str, Any]) -> Cursor:
    seen = row["first_seen_at"]
    if isinstance(seen, str):
        seen = datetime.fromisoformat(seen)
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)
    return Cursor(int(row["_priority_rank"]), seen, str(row["occurrence_id"]))


def options_query(adapter: str, relation: str, population: str, limit: int = 500) -> Query:
    """Filter choices over the whole population: statuses, owners, tags and the created range."""
    if adapter == "bigquery":
        tags = (f"select distinct trim(tag) from {relation}, unnest(json_value_array(test_tags)) tag "
                f"where {population} and tag is not null and trim(tag) <> '' and lower(trim(tag)) <> 'dqm'")
    else:
        elements = (
            "case when jsonb_typeof(test_tags::jsonb) = 'array' then test_tags::jsonb "
            "else '[]'::jsonb end"
        )
        tags = (f"select distinct trim(tag) from {relation} "
                f"cross join lateral jsonb_array_elements_text({elements}) tag "
                f"where {population} and trim(tag) <> '' and lower(trim(tag)) <> 'dqm'")
    return Query(
        f"select array(select distinct {_status_sql()} from {relation} where {population} "
        f"order by 1 limit {limit}) as statuses, "
        f"array(select distinct {_owner_sql()} from {relation} where {population} "
        f"order by 1 limit {limit}) as owners, "
        f"array(select * from ({tags}) t order by 1 limit {limit}) as tags, "
        f"(select min(first_seen_at) from {relation} where {population}) as first_seen_min, "
        f"(select max(first_seen_at) from {relation} where {population}) as first_seen_max"
    )
