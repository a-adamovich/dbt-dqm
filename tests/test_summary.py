import sqlite3

import pytest

from dbt_dqm_app.browse import STATUS_ORDER
from dbt_dqm_app.config import AppConfig
from dbt_dqm_app.errors import SnapshotUnavailable
from dbt_dqm_app.summary import owner_summary, print_summary


@pytest.fixture(params=["postgres", "bigquery"])
def summary_db(tmp_path, monkeypatch, request):
    """Execute the aggregate itself, rather than mocking its calculated counts."""
    config = AppConfig(
        tmp_path, tmp_path, "dev", "demo", request.param, "db", "schema", "US", "oauth", None
    )
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        create table dqm_issue_occurrences (
            occurrence_id text primary key, dqm_owner text, record_status text, workflow_status text);
        create table dqm_issue_events (
            occurrence_id text, test_unique_id text, invocation_id text, event_type text);
        create table dqm_reconciliation_inputs (
            run_id text, test_unique_id text, invocation_id text);
        create table dqm_reconciliation_runs (
            run_id text, status text, generation_at_freeze integer);
        insert into dqm_reconciliation_runs values ('run', 'completed', 3);
        insert into dqm_reconciliation_inputs values ('run', 'test.a', 'capture');
    """)
    queries = []

    def read(config, query):
        queries.append(query)
        sql = query.sql.replace("%(run_id)s", ":run_id")
        return [dict(row) for row in db.execute(sql, query.params)], ("install", 4)

    monkeypatch.setattr("dbt_dqm_app.warehouse._table", lambda config, name: name)
    monkeypatch.setattr("dbt_dqm_app.warehouse._read_with_state", read)
    yield config, db, queries
    db.close()


def test_owner_changes_workload_and_saved_ownership(summary_db, capsys):
    config, db, queries = summary_db
    db.executemany("insert into dqm_issue_occurrences values (?,?,?,?)", [
        ("opened", "reviewer-edited", "Active", " new "),
        ("recurring", "a, b", "Active", "IN_PROGRESS"),
        ("closed", "archived-only", "Archived", "RESOLVED"),
        ("unchanged", "reviewer-edited", "Active", "RESOLVED"),
        ("unassigned", "  ", "Active", None),
        ("old-archive", "excluded", "Archived", "NEW"),
        ("structural", "excluded", "Archived", "NEW"),
    ])
    db.executemany("insert into dqm_issue_events values (?,?,?,?)", [
        ("opened", "test.a", "capture", "APPEARED"),
        ("recurring", "test.a", "capture", "REAPPEARED"),
        ("closed", "test.a", "capture", "DISAPPEARED"),
        ("unchanged", "test.a", "capture", "VALUES_CHANGED"),
        ("unassigned", "test.a", "capture", "STILL_FAILING"),
        ("old-archive", "test.a", "old-capture", "DISAPPEARED"),
        ("structural", "test.a", "capture", "CLOSED_STRUCTURAL"),
        # The same invocation's evidence for an unfrozen test must not be counted.
        ("unchanged", "test.other", "capture", "REAPPEARED"),
    ])
    result = owner_summary(config, "run")
    assert {key: result[key] for key in ("opened", "recurring", "archived", "new", "active")} == {
        "opened": 1, "recurring": 1, "archived": 1, "new": 2, "active": 4,
    }
    assert [row["owner"] for row in result["owners"]] == [
        "reviewer-edited", "Unassigned", "a, b", "archived-only",
    ]
    closed = result["owners"][-1]
    assert closed["archived"] == 1 and closed["active"] == closed["new"] == 0
    assert result["reconciliation_id"] == "run" and result["generation"] == ("install", 4)
    assert len(queries) == 1 and queries[0].params == {"run_id": "run"}
    print_summary(result)
    lines = capsys.readouterr().out.splitlines()
    assert lines[2].startswith("Total")
    assert lines[1].index("OPENED (NEW)") < lines[1].index("RECURRING") < lines[1].index("ARCHIVED")
    assert lines[1].index("ARCHIVED") < lines[1].index("NEW (status)") < lines[1].index("ACTIVE")
    assert "changes applied by this reconciliation" in lines[-1]


def test_pending_captures_and_multiple_categories_count_each_occurrence_once(summary_db):
    config, db, _ = summary_db
    db.execute("insert into dqm_issue_occurrences values ('one', 'owner', 'Archived', 'NEW')")
    db.executemany("insert into dqm_reconciliation_inputs values ('run','test.a',?)", [
        ("pending-1",), ("pending-2",),
    ])
    db.executemany("insert into dqm_issue_events values ('one','test.a',?,?)", [
        ("pending-1", "APPEARED"), ("pending-2", "DISAPPEARED"),
        ("pending-2", "APPEARED"),
    ])
    result = owner_summary(config, "run")
    assert result["opened"] == result["archived"] == 1
    assert result["new"] == result["active"] == result["recurring"] == 0
    # A later reconciliation with no new evidence doesn't count old lifecycle events again.
    db.execute("insert into dqm_reconciliation_runs values ('retry','completed',3)")
    retry = owner_summary(config, "retry")
    assert retry["opened"] == retry["recurring"] == retry["archived"] == 0


@pytest.mark.parametrize("status", [*STATUS_ORDER, "IGNORED", "", None])
def test_active_workflow_statuses(summary_db, status):
    config, db, _ = summary_db
    db.execute("insert into dqm_issue_occurrences values ('one',null,'Active',?)", (status,))
    result = owner_summary(config, "run")
    assert result["active"] == 1
    assert result["new"] == (1 if status in ("NEW", "", None) else 0)
    assert result["opened"] == result["recurring"] == result["archived"] == 0


def test_empty_summary(summary_db):
    config, _, _ = summary_db
    result = owner_summary(config, "run")
    assert result["owners"] == [
        {"owner": "Unassigned", "opened": 0, "recurring": 0, "archived": 0, "new": 0, "active": 0}
    ]


@pytest.mark.parametrize("status, frozen", [
    ("started", 3), ("abandoned", 3), ("completed", 2), ("completed", None),
])
def test_stale_or_incomplete_reconciliation_is_refused(summary_db, status, frozen):
    config, db, _ = summary_db
    db.execute("update dqm_reconciliation_runs set status=?,generation_at_freeze=?", (status, frozen))
    with pytest.raises(SnapshotUnavailable, match="generation has changed"):
        owner_summary(config, "run")


def test_installation_reset_missing_run_is_refused(summary_db):
    config, db, _ = summary_db
    db.execute("delete from dqm_reconciliation_runs")
    with pytest.raises(SnapshotUnavailable):
        owner_summary(config, "run")
