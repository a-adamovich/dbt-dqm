"""Warehouse browsing execution: verified pages, the BigQuery search ceiling, summaries."""

from datetime import UTC, datetime

import pytest
from google.api_core.exceptions import Forbidden

from dbt_dqm_app import browse, warehouse
from dbt_dqm_app.config import AppConfig
from dbt_dqm_app.errors import SearchTooCostly, SnapshotUnavailable


def config(tmp_path, adapter="bigquery", max_search_gib=1.0):
    return AppConfig(tmp_path, tmp_path, "dev", "demo", adapter, "database", "schema", "US",
                     "oauth", None, max_search_gib=max_search_gib)


def test_max_search_gib_is_validated(tmp_path):
    assert config(tmp_path, max_search_gib=0.5).max_search_bytes == 2**29
    for invalid in (0, -1, 2048):
        with pytest.raises(ValueError):
            config(tmp_path, max_search_gib=invalid)


class FakeJob:
    def __init__(self, rows=(), bytes_processed=0, error=None):
        self.rows, self.total_bytes_processed, self.error = list(rows), bytes_processed, error

    def result(self):
        if self.error:
            raise self.error
        return [FakeRow(row) for row in self.rows]


class FakeRow(dict):
    pass


class FakeClient:
    def __init__(self, estimate, rows=(), error=None):
        self.estimate, self.rows, self.error, self.calls = estimate, rows, error, []

    def query(self, sql, job_config):
        self.calls.append(job_config)
        if job_config.dry_run:
            return FakeJob(bytes_processed=self.estimate)
        return FakeJob(self.rows, error=self.error)


QUERY = browse.Query("select 1", {"search": "abc", "ids": ["a"], "page_limit": 26})


def test_a_search_above_the_ceiling_is_refused_before_it_runs(tmp_path, monkeypatch):
    client = FakeClient(estimate=3 * 2**30)
    monkeypatch.setattr(warehouse, "client_for", lambda config: client)
    with pytest.raises(SearchTooCostly, match="about 3.00 GiB.*1 GiB limit"):
        warehouse._run_browse_query(config(tmp_path), QUERY, searched=True)
    assert [call.dry_run for call in client.calls] == [True]


def test_a_search_runs_with_maximum_bytes_billed(tmp_path, monkeypatch):
    client = FakeClient(estimate=2**20, rows=[{"matching": 4}])
    monkeypatch.setattr(warehouse, "client_for", lambda config: client)
    rows = warehouse._run_browse_query(config(tmp_path, max_search_gib=2), QUERY, searched=True)
    assert rows == [{"matching": 4}]
    assert client.calls[1].maximum_bytes_billed == 2 * 2**30
    names = {parameter.name for parameter in client.calls[1].query_parameters}
    assert names == {"search", "ids", "page_limit"}


def test_billing_refusal_is_reported_as_too_costly(tmp_path, monkeypatch):
    error = Forbidden("Query exceeded limit for bytes billed",
                      errors=[{"reason": "bytesBilledLimitExceeded"}])
    client = FakeClient(estimate=2**20, error=error)
    monkeypatch.setattr(warehouse, "client_for", lambda config: client)
    with pytest.raises(SearchTooCostly):
        warehouse._run_browse_query(config(tmp_path), QUERY, searched=True)


def test_pages_without_search_have_no_dry_run_or_ceiling(tmp_path, monkeypatch):
    client = FakeClient(estimate=10 * 2**40, rows=[])
    monkeypatch.setattr(warehouse, "client_for", lambda config: client)
    warehouse._run_browse_query(config(tmp_path), QUERY)
    assert len(client.calls) == 1 and client.calls[0].maximum_bytes_billed is None


def _issue(occurrence_id, rank=0, generation=1, setup="ready"):
    return {"_generation": generation, "_setup_status": setup, "occurrence_id": occurrence_id,
            "first_seen_at": datetime(2026, 1, 1, tzinfo=UTC), "record_values_json": '{"a": 1}',
            "_priority_rank": rank, "_status_rank": 0}


class Statements:
    """Answers each browse statement in turn; records what ran."""

    def __init__(self, *answers):
        self.answers, self.seen = list(answers), []

    def __call__(self, config, query, searched=False):
        self.seen.append((query, searched))
        return self.answers.pop(0)


def test_every_read_carries_the_control_state_in_the_same_statement(tmp_path, monkeypatch):
    run = Statements([_issue("a")])
    monkeypatch.setattr(warehouse, "_run_browse_query", run)
    warehouse.browse_page(config(tmp_path, "postgres"), "true", browse.Filters(), None, 25)
    sql = run.seen[0][0].sql
    assert sql.startswith("select s.*, q.* from (select max(generation) as _generation")
    assert "left join (select *" in sql and sql.endswith("q.occurrence_id")


def test_bigquery_page_reads_keys_then_full_rows_in_key_order(tmp_path, monkeypatch):
    run = Statements([_issue("b"), _issue("a"), _issue("c")],
                     [_issue("c"), _issue("a"), _issue("b")])
    monkeypatch.setattr(warehouse, "_run_browse_query", run)
    page = warehouse.browse_page(config(tmp_path), "true", browse.Filters(search="abc"), None, 25)
    assert [row["occurrence_id"] for row in page.rows] == ["b", "a", "c"]
    assert page.next_cursor is None and page.generation == 1
    assert "_priority_rank" not in page.rows[0] and "_generation" not in page.rows[0]
    assert run.seen[1][0].params["ids"] == ["b", "a", "c"]
    assert [searched for _, searched in run.seen] == [True, False]  # only phase 1 searches


def test_an_empty_page_still_reports_its_generation(tmp_path, monkeypatch):
    empty = {"_generation": 7, "_setup_status": "ready", "occurrence_id": None}
    monkeypatch.setattr(warehouse, "_run_browse_query", Statements([empty]))
    page = warehouse.browse_page(config(tmp_path), "true", browse.Filters(), None, 25)
    assert page.rows == [] and page.generation == 7 and page.next_cursor is None


def test_page_reports_the_next_cursor(tmp_path, monkeypatch):
    rows = [_issue(f"id{i:03}", rank=1) for i in range(26)]
    monkeypatch.setattr(warehouse, "_run_browse_query", Statements(rows))
    page = warehouse.browse_page(config(tmp_path, "postgres"), "true", browse.Filters(), None, 25)
    assert len(page.rows) == 25
    assert page.next_cursor == browse.Cursor(1, 0, datetime(2026, 1, 1, tzinfo=UTC), "id024")


def test_a_reconciliation_between_the_two_bigquery_reads_is_retried_once(tmp_path, monkeypatch):
    run = Statements([_issue("a")], [_issue("a", generation=2)],
                     [_issue("a", generation=2)], [_issue("a", generation=2)])
    monkeypatch.setattr(warehouse, "_run_browse_query", run)
    page = warehouse.browse_page(config(tmp_path), "true", browse.Filters(), None, 25)
    assert page.generation == 2 and len(run.seen) == 4


def test_repeated_reconciliations_are_reported(tmp_path, monkeypatch):
    run = Statements([_issue("a")], [_issue("a", generation=2)],
                     [_issue("a", generation=2)], [_issue("a", generation=3)])
    monkeypatch.setattr(warehouse, "_run_browse_query", run)
    with pytest.raises(SnapshotUnavailable, match="restart from the first page"):
        warehouse.browse_page(config(tmp_path), "true", browse.Filters(), None, 25)


def test_a_row_missing_from_the_second_read_is_retried(tmp_path, monkeypatch):
    run = Statements([_issue("a"), _issue("b")], [_issue("a")],
                     [_issue("a")], [_issue("a")])
    monkeypatch.setattr(warehouse, "_run_browse_query", run)
    page = warehouse.browse_page(config(tmp_path), "true", browse.Filters(), None, 25)
    assert [row["occurrence_id"] for row in page.rows] == ["a"]


def test_setup_in_progress_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(warehouse, "_run_browse_query",
                        Statements([_issue("a", setup="migrating")]))
    with pytest.raises(SnapshotUnavailable, match="setup or a migration"):
        warehouse.browse_page(config(tmp_path, "postgres"), "true", browse.Filters(), None, 25)


def test_summary_is_one_statement_with_global_metrics_and_the_filtered_count(tmp_path, monkeypatch):
    run = Statements([{"_generation": 5, "_setup_status": "ready", "active_count": 10,
                       "new_count": 4, "blocked_count": None, "recurring_count": 1,
                       "matching": 3}])
    monkeypatch.setattr(warehouse, "_run_browse_query", run)
    summary = warehouse.browse_summary(config(tmp_path), "true", browse.Filters(search="abc"))
    assert summary == {"active_count": 10, "new_count": 4, "blocked_count": 0,
                       "recurring_count": 1, "matching": 3, "generation": 5}
    assert len(run.seen) == 1 and run.seen[0][1] is True
    assert "countif" in run.seen[0][0].sql and "as matching" in run.seen[0][0].sql
