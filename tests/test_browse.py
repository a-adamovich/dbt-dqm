"""Warehouse browsing SQL: parameterized, bounded, and equivalent to the snapshot-mode filters."""

import json
import os
import random
import uuid
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from dbt_dqm_app.browse import (
    SEARCH_COLUMNS,
    Cursor,
    Filters,
    InvalidSearch,
    count_query,
    created_bounds,
    cursor_from_row,
    metrics_query,
    page_query,
    rows_by_id_query,
)
from dbt_dqm_app.display import owner_label, priority_rank, tag_list

HOSTILE = "x'); drop table issues; -- %_\\ \"quoted\" ünïcode"


@pytest.mark.parametrize("adapter", ["postgres", "bigquery"])
def test_user_input_is_never_part_of_the_sql(adapter):
    filters = Filters(
        lifecycle=("Active", HOSTILE), statuses=(HOSTILE,), owners=(HOSTILE,),
        priorities=(HOSTILE,), tags=(HOSTILE,), search=HOSTILE[:150],
        created_from=date(2026, 1, 1), created_to=date(2026, 1, 31),
    )
    cursor = Cursor(1, 2, datetime(2026, 1, 1, tzinfo=UTC), HOSTILE)
    for query in (page_query(adapter, "issues", "true", filters, cursor, 25),
                  count_query(adapter, "issues", "true", filters)):
        assert "drop table" not in query.sql and "ünïcode" not in query.sql
        # The hostile value travels only as a parameter.
        assert any(
            value == HOSTILE or (isinstance(value, list) and HOSTILE in value)
            for value in query.params.values()
        )


def test_search_is_bounded():
    with pytest.raises(InvalidSearch):
        page_query("postgres", "issues", "true", Filters(search="ab"), None, 25)
    with pytest.raises(InvalidSearch):
        page_query("postgres", "issues", "true", Filters(search="x" * 201), None, 25)
    assert page_query("postgres", "issues", "true", Filters(search="   "), None, 25).params.get("search") is None


def test_postgres_search_escapes_wildcards():
    query = page_query("postgres", "issues", "true", Filters(search="50%_off\\"), None, 25)
    assert query.params["search"] == "%50\\%\\_off\\\\%"


def test_page_size_and_adapter_are_validated():
    with pytest.raises(ValueError):
        page_query("postgres", "issues", "true", Filters(), None, 37)
    with pytest.raises(ValueError):
        page_query("snowflake", "issues", "true", Filters(), None, 25)


def test_created_bounds_cover_whole_local_days():
    tz = timezone(timedelta(hours=-5))
    lower, upper = created_bounds(date(2026, 3, 1), date(2026, 3, 2), tz)
    assert lower == datetime(2026, 3, 1, 5, tzinfo=UTC)
    assert upper == datetime(2026, 3, 3, 5, tzinfo=UTC)


def test_bigquery_keyset_and_metrics_shape():
    cursor = Cursor(0, 1, datetime(2026, 1, 1, tzinfo=UTC), "abc")
    sql = page_query("bigquery", "`p.d.v`", "true", Filters(), cursor, 50).sql
    assert "@after_priority" in sql and "@after_id" in sql and " or " in sql
    assert "countif" in metrics_query("bigquery", "`p.d.v`", "true").sql
    assert "unnest(@ids)" in rows_by_id_query("bigquery", "`p.d.v`", ["a"]).sql


# --- Equivalence with the snapshot-mode filters, on a real Postgres --------------------------------

DSN = os.environ.get("DBT_DQM_TEST_DSN")
STATUSES = [None, "NEW", "new", "BLOCKED", "IN_PROGRESS", "TRIAGED", "RESOLVED", "custom_status"]
PRIORITIES = [None, "critical", "HIGH", "high", "medium", "low", "unset", "weird"]
OWNERS = [None, "", "   ", "alice", " bob ", "carol"]
TAGS = [None, "[]", '["finance"]', '["finance", "ops"]', '[" ops "]', '["eu", "finance"]']


def _rows(seed=7, count=1500):
    rng = random.Random(seed)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    for _ in range(count):
        rows.append({
            "occurrence_id": uuid.UUID(int=rng.getrandbits(128)).hex,
            "record_status": rng.choice(["Active", "Active", "Archived"]),
            "workflow_status": rng.choice(STATUSES),
            "test_priority": rng.choice(PRIORITIES),
            "poc_responsible": rng.choice(OWNERS),
            "test_tags": rng.choice(TAGS),
            "is_recurrence": rng.choice([None, True, False]),
            # Coarse times give many ties, which the keyset must break by occurrence_id.
            "first_seen_at": base + timedelta(hours=rng.randrange(0, 24 * 40, 6)),
            "test_name": rng.choice(["email_check", "amount_check", "line_check"]),
            "unique_id": uuid.UUID(int=rng.getrandbits(128)).hex[:12],
            "record_values_json": json.dumps({"reason": rng.choice(["Missing SKU", "50% off", "a_b"])}),
            "call_to_action": rng.choice([None, "Fix it", "Investigate"]),
            "notes": rng.choice([None, "checked", "Needs REVIEW"]),
            "ticket_url": None,
        })
    return rows


def _reference(rows, filters):
    """The snapshot-mode semantics from streamlit_app._render_issues."""
    lower, upper = created_bounds(filters.created_from, filters.created_to)
    selected = []
    for row in rows:
        status = (row["workflow_status"] or "NEW").upper()
        if filters.lifecycle and row["record_status"] not in filters.lifecycle:
            continue
        if filters.statuses and status not in filters.statuses:
            continue
        if filters.owners and owner_label(row["poc_responsible"]) not in filters.owners:
            continue
        if filters.priorities and (row["test_priority"] or "unset") not in filters.priorities:
            continue
        if filters.tags and not set(tag_list(row["test_tags"])) & set(filters.tags):
            continue
        if filters.recurrences_only and not row["is_recurrence"]:
            continue
        if lower and row["first_seen_at"] < lower or upper and row["first_seen_at"] >= upper:
            continue
        if filters.search:
            text = " ".join(str(row[column] or "") for column in SEARCH_COLUMNS).lower()
            if filters.search.strip().lower() not in text:
                continue
        selected.append(row)
    ranks = {"NEW": 0, "BLOCKED": 1, "IN_PROGRESS": 2, "TRIAGED": 3, "RESOLVED": 4,
             "ACCEPTED_RISK": 5, "FALSE_POSITIVE": 6}
    selected.sort(key=lambda row: (
        priority_rank(row["test_priority"]), ranks.get((row["workflow_status"] or "NEW").upper(), 99),
        row["first_seen_at"], row["occurrence_id"],
    ))
    return [row["occurrence_id"] for row in selected]


def _random_filters(rng):
    """Each filter is applied only some of the time, so most combinations match several pages."""

    def maybe(values, k):
        return tuple(rng.sample(values, k=rng.randint(1, k))) if rng.random() < 0.3 else ()

    start = date(2026, 1, 1) + timedelta(days=rng.randrange(0, 20))
    return Filters(
        lifecycle=maybe(["Active", "Archived"], 2),
        statuses=maybe(["NEW", "BLOCKED", "IN_PROGRESS", "TRIAGED", "RESOLVED", "CUSTOM_STATUS"], 4),
        owners=maybe(["Unassigned", "alice", "bob", "carol"], 3),
        priorities=maybe(["critical", "high", "HIGH", "medium", "low", "unset", "weird"], 4),
        tags=maybe(["finance", "ops", "eu"], 2),
        recurrences_only=rng.random() < 0.15,
        created_from=start if rng.random() < 0.25 else None,
        created_to=start + timedelta(days=rng.randrange(5, 20)) if rng.random() < 0.25 else None,
        search=rng.choice([None] * 6 + ["check", "REVIEW", "50% off", "a_b", "sku"]),
    )


@pytest.mark.skipif(not DSN, reason="Set DBT_DQM_TEST_DSN for the Postgres equivalence test")
def test_postgres_pages_match_the_snapshot_filters():
    import psycopg2
    from psycopg2.extras import RealDictCursor

    rows = _rows()
    schema = "dqm_browse_" + uuid.uuid4().hex[:10]
    columns = list(rows[0])
    with psycopg2.connect(DSN) as connection, connection.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f'create schema "{schema}"')
        try:
            cur.execute(f'''create table "{schema}".issues (occurrence_id text, record_status text,
                workflow_status text, test_priority text, poc_responsible text, test_tags text,
                is_recurrence boolean, first_seen_at timestamp, test_name text, unique_id text,
                record_values_json text, call_to_action text, notes text, ticket_url text)''')
            for row in rows:
                values = {**row, "first_seen_at": row["first_seen_at"].replace(tzinfo=None)}
                cur.execute(
                    f'insert into "{schema}".issues ({", ".join(columns)}) values '
                    f'({", ".join(f"%({c})s" for c in columns)})', values)
            relation = f'"{schema}".issues'
            rng = random.Random(11)
            sizes = []
            for _ in range(60):
                filters = _random_filters(rng)
                expected = _reference(rows, filters)
                sizes.append(len(expected))
                got, cursor = [], None
                while True:
                    query = page_query("postgres", relation, "true", filters, cursor, 25)
                    cur.execute(query.sql, query.params)
                    page = cur.fetchall()
                    got += [row["occurrence_id"] for row in page[:25]]
                    if len(page) <= 25:
                        break
                    cursor = cursor_from_row(page[24])
                assert got == expected, filters
                count = count_query("postgres", relation, "true", filters)
                cur.execute(count.sql, count.params)
                assert cur.fetchone()["matching"] == len(expected), filters
            # The comparison is only meaningful if most combinations page through results.
            assert sum(size > 25 for size in sizes) >= 30, sizes
            assert sum(size == 0 for size in sizes) <= 10, sizes
            metrics = metrics_query("postgres", relation, "true")
            cur.execute(metrics.sql, metrics.params)
            values = cur.fetchone()
            active = [row for row in rows if row["record_status"] == "Active"]
            assert values["active_count"] == len(active)
            assert values["new_count"] == sum((row["workflow_status"] or "NEW").upper() == "NEW" for row in active)
            assert values["blocked_count"] == sum((row["workflow_status"] or "").upper() == "BLOCKED" for row in active)
            assert values["recurring_count"] == sum(bool(row["is_recurrence"]) for row in active)
        finally:
            cur.execute(f'drop schema "{schema}" cascade')


BIGQUERY_PROJECT = os.environ.get("DBT_DQM_BIGQUERY_TEST_PROJECT")


@pytest.mark.skipif(not BIGQUERY_PROJECT, reason="Set DBT_DQM_BIGQUERY_TEST_PROJECT for BigQuery")
def test_bigquery_two_phase_pages_match_the_snapshot_filters():
    from google.cloud import bigquery

    from dbt_dqm_app.browse import KEY_COLUMNS

    rows = _rows()
    client = bigquery.Client(project=BIGQUERY_PROJECT, location="US")
    dataset = f"{BIGQUERY_PROJECT}.dqm_browse_{uuid.uuid4().hex[:10]}"
    created = bigquery.Dataset(dataset)
    created.location = "US"
    client.create_dataset(created)
    try:
        schema = [bigquery.SchemaField(name, kind) for name, kind in (
            ("occurrence_id", "STRING"), ("record_status", "STRING"), ("workflow_status", "STRING"),
            ("test_priority", "STRING"), ("poc_responsible", "STRING"), ("test_tags", "STRING"),
            ("is_recurrence", "BOOL"), ("first_seen_at", "TIMESTAMP"), ("test_name", "STRING"),
            ("unique_id", "STRING"), ("record_values_json", "STRING"), ("call_to_action", "STRING"),
            ("notes", "STRING"), ("ticket_url", "STRING"),
        )]
        payload = [{**row, "first_seen_at": row["first_seen_at"].isoformat()} for row in rows]
        client.load_table_from_json(
            payload, f"{dataset}.issues", job_config=bigquery.LoadJobConfig(schema=schema)
        ).result()
        relation = f"`{dataset}.issues`"

        def run(query):
            parameters = []
            for name, value in query.params.items():
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
            config = bigquery.QueryJobConfig(query_parameters=parameters)
            return [dict(row.items()) for row in client.query(query.sql, job_config=config).result()]

        rng = random.Random(11)
        for _ in range(20):
            filters = _random_filters(rng)
            expected = _reference(rows, filters)
            got, cursor = [], None
            while True:
                keys = run(page_query("bigquery", relation, "true", filters, cursor, 200,
                                      columns=", ".join(KEY_COLUMNS)))
                ids = [row["occurrence_id"] for row in keys[:200]]
                if ids:  # phase 2: full rows for at most one page, reordered like phase 1
                    full = {row["occurrence_id"]: row for row in run(rows_by_id_query("bigquery", relation, ids))}
                    assert set(full) == set(ids)
                got += ids
                if len(keys) <= 200:
                    break
                cursor = cursor_from_row(keys[199])
            assert got == expected, filters
            assert run(count_query("bigquery", relation, "true", filters))[0]["matching"] == len(expected)
        values = run(metrics_query("bigquery", relation, "true"))[0]
        active = [row for row in rows if row["record_status"] == "Active"]
        assert values["active_count"] == len(active)
        assert values["recurring_count"] == sum(bool(row["is_recurrence"]) for row in active)
    finally:
        client.delete_dataset(dataset, delete_contents=True, not_found_ok=True)
