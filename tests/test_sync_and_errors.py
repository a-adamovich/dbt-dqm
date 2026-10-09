import subprocess

import pytest
from google.api_core.exceptions import BadRequest
from psycopg2 import errors as pg_errors

from dbt_dqm_app import warehouse
from dbt_dqm_app.config import AppConfig
from dbt_dqm_app.errors import AppError, SnapshotUnavailable, WarehouseBusy, classify


@pytest.fixture(autouse=True)
def streamed_fake(monkeypatch):
    # The legacy fake answers both small metadata reads and the new issue stream. Production
    # adapter streaming is tested independently in test_cache_limits.
    def streamed(config, query):
        yield from warehouse._query_rows(config, query)

    monkeypatch.setattr(warehouse, "_iter_issue_rows", streamed)


def config(tmp_path, adapter="postgres", archive_cache_days=90):
    return AppConfig(
        tmp_path,
        tmp_path,
        "dev",
        "demo",
        adapter,
        "database",
        "schema",
        "US",
        "oauth",
        None,
        archive_cache_days=archive_cache_days,
    )


class FakeWarehouse:
    """Answers the sync queries in order: state before, view rows, state after."""

    def __init__(self, before, rows, after):
        self.answers = [[before], rows, [after]]
        self.queries = []

    def __call__(self, config, query):
        self.queries.append(query)
        return self.answers.pop(0)


def issue(occurrence_id):
    return {
        "occurrence_id": occurrence_id,
        "test_name": "t",
        "record_values_json": '{"id":"1"}',
    }


def state(generation=7, setup_status="ready", issue_count=0):
    return {"generation": generation, "setup_status": setup_status, "issue_count": issue_count}


def test_verified_snapshot_is_returned(tmp_path, monkeypatch):
    fake = FakeWarehouse(state(issue_count=2), [issue("a"), issue("b")], state(issue_count=2))
    monkeypatch.setattr(warehouse, "_query_rows", fake)
    assert [row["occurrence_id"] for row in warehouse.fetch_issues(config(tmp_path))] == ["a", "b"]
    # The view rows and the direct table count use the same fixed archive cutoff.
    cutoff = fake.queries[0].split("archived_at >= ")[1].split(")")[0]
    assert all(cutoff in query for query in fake.queries)


def test_verified_empty_snapshot_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(warehouse, "_query_rows", FakeWarehouse(state(), [], state()))
    assert warehouse.fetch_issues(config(tmp_path)) == []


def test_view_disagreeing_with_tracking_table_is_rejected(tmp_path, monkeypatch):
    fake = FakeWarehouse(state(issue_count=3), [], state(issue_count=3))
    monkeypatch.setattr(warehouse, "_query_rows", fake)
    with pytest.raises(SnapshotUnavailable, match="returned 0 issues but the tracking table holds 3"):
        warehouse.fetch_issues(config(tmp_path))


def test_reconciliation_during_sync_is_rejected(tmp_path, monkeypatch):
    fake = FakeWarehouse(state(generation=7), [], state(generation=8))
    monkeypatch.setattr(warehouse, "_query_rows", fake)
    with pytest.raises(SnapshotUnavailable, match="changed while syncing"):
        warehouse.fetch_issues(config(tmp_path))


def test_setup_in_progress_is_rejected_before_reading_issues(tmp_path, monkeypatch):
    fake = FakeWarehouse(state(setup_status="migrating"), [], state())
    monkeypatch.setattr(warehouse, "_query_rows", fake)
    with pytest.raises(SnapshotUnavailable, match="migration is in progress"):
        warehouse.fetch_issues(config(tmp_path))
    assert len(fake.queries) == 1


def test_active_only_cache_uses_no_archive_cutoff(tmp_path, monkeypatch):
    fake = FakeWarehouse(state(), [], state())
    monkeypatch.setattr(warehouse, "_query_rows", fake)
    warehouse.fetch_issues(config(tmp_path, archive_cache_days=0))
    assert all("archived_at" not in query for query in fake.queries)


def test_postgres_connections_carry_the_lock_timeout(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(warehouse.psycopg2, "connect", lambda **kwargs: seen.update(kwargs))
    settings = config(tmp_path)
    warehouse._postgres_connection(settings)
    assert seen["options"] == f"-c lock_timeout={settings.lock_timeout_seconds * 1000}"


def test_apply_refresh_failure_still_reports_the_committed_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(warehouse, "validate_dbt", lambda config: None)
    monkeypatch.setattr(warehouse, "apply_patches", lambda config, patches: "batch-1")

    def unavailable(config):
        raise SnapshotUnavailable("views out of date")

    monkeypatch.setattr(warehouse, "fetch_issues", unavailable)
    batch_id, snapshot, refresh_error = warehouse.apply_worker(config(tmp_path), [])
    assert (batch_id, snapshot, refresh_error.message) == ("batch-1", None, "views out of date")


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("Could not find profile named 'demo'", "couldn't find the selected profile"),
        ("FATAL: password authentication failed for user", "couldn't sign in"),
        ("could not connect to server: Connection refused", "couldn't reach the warehouse"),
        ("Parsing Error\n  something is wrong", "couldn't parse the project"),
        ("something unexpected", "`dbt debug` failed."),
    ],
)
def test_dbt_failures_get_actionable_messages(output, expected):
    error = subprocess.CalledProcessError(2, ["dbt", "debug", "--target", "dev"], output, "")
    failure = classify(error)
    assert expected in failure.message
    assert output in failure.details


def test_lock_timeouts_and_bigquery_conflicts_mean_busy():
    lock = pg_errors.LockNotAvailable("canceling statement due to lock timeout")
    assert isinstance(classify(lock), WarehouseBusy)
    conflict = BadRequest("Transaction is aborted due to concurrent update against table")
    assert isinstance(classify(conflict), WarehouseBusy)
    assert not isinstance(classify(BadRequest("Syntax error")), WarehouseBusy)


def test_unknown_errors_keep_their_details():
    failure = classify(RuntimeError("boom"))
    assert type(failure) is AppError
    assert "boom" in failure.message and "RuntimeError" in failure.details
