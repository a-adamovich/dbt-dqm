import json
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from dbt_dqm_app import warehouse
from dbt_dqm_app.config import AppConfig
from dbt_dqm_app.limits import (
    MAX_CACHE_ISSUES,
    MAX_CACHE_MIB,
    CacheLimitExceeded,
    check_cache_size,
    issue_json,
)
from dbt_dqm_app.store import Workspace


def config(tmp_path, **kwargs):
    return AppConfig(tmp_path, tmp_path, "dev", "demo", "postgres", "db", "schema", "US",
                     "oauth", None, **kwargs)


def issue(identity, value="é"):
    return {"occurrence_id": identity, "notes": None, "annotation_version": 0,
            "record_values_json": json.dumps({"id": identity, "value": value})}


@pytest.mark.parametrize("kwargs", [{"max_cache_issues": 0}, {"max_cache_issues": 50001},
                                   {"max_cache_mib": -1}, {"max_cache_mib": 129}])
def test_config_rejects_unsupported_limits(tmp_path, kwargs):
    with pytest.raises(ValueError, match="max-cache"):
        config(tmp_path, **kwargs)


def test_supported_ceiling_accepts_exact_boundary_and_refuses_one_over():
    budget = MAX_CACHE_MIB * 2**20
    check_cache_size(MAX_CACHE_ISSUES, budget, MAX_CACHE_ISSUES, budget)
    with pytest.raises(CacheLimitExceeded):
        check_cache_size(MAX_CACHE_ISSUES + 1, budget, MAX_CACHE_ISSUES, budget)
    with pytest.raises(CacheLimitExceeded):
        check_cache_size(MAX_CACHE_ISSUES, budget + 1, MAX_CACHE_ISSUES, budget)


def test_full_128_mib_snapshot_boundary_is_atomic(tmp_path):
    workspace = Workspace(tmp_path / "boundary.sqlite")
    budget = MAX_CACHE_MIB * 2**20

    def rows(extra=0):
        # Produce one row at a time: the boundary test itself never creates a 128 MiB list.
        for index in range(64):
            row = issue(str(index), "")
            allowance = budget // 64 + (extra if index == 63 else 0)
            overhead = len(issue_json(row).encode("utf-8"))
            yield issue(str(index), "x" * (allowance - overhead))

    workspace.replace_synced_data(rows(), {"tests": ["accepted"]})
    assert workspace.cache_size() == (64, budget)
    before = workspace.last_sync(), workspace.health_rows()
    with pytest.raises(CacheLimitExceeded):
        workspace.replace_synced_data(rows(extra=1), {"tests": ["must not install"]})
    assert workspace.cache_size() == (64, budget)
    assert (workspace.last_sync(), workspace.health_rows()) == before


def test_preflight_overflow_never_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(warehouse, "_snapshot_state", lambda *a: {
        "setup_status": "ready", "generation": 1, "issue_count": 3})
    download = MagicMock()
    monkeypatch.setattr(warehouse, "_iter_issue_rows", download)
    with pytest.raises(CacheLimitExceeded, match="3 issues"):
        warehouse.fetch_issues(config(tmp_path, max_cache_issues=2))
    download.assert_not_called()


def test_stream_stops_on_first_oversized_row_and_closes(tmp_path, monkeypatch):
    monkeypatch.setattr(warehouse, "_snapshot_state", lambda *a: {
        "setup_status": "ready", "generation": 1, "issue_count": 1})
    seen = []

    def download(*args):
        try:
            seen.append("first")
            yield issue("one", "x" * 2**20)
            seen.append("unreachable")
            yield issue("two")
        finally:
            seen.append("closed")

    monkeypatch.setattr(warehouse, "_iter_issue_rows", download)
    with pytest.raises(CacheLimitExceeded, match="serialized bytes"):
        warehouse.fetch_issues(config(tmp_path, max_cache_mib=1))
    assert seen == ["first", "closed"]


def test_count_race_cannot_install_partial_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(warehouse, "_snapshot_state", lambda *a: {
        "setup_status": "ready", "generation": 1, "issue_count": 2})
    monkeypatch.setattr(warehouse, "_iter_issue_rows", lambda *a: (issue(str(i)) for i in range(3)))
    with pytest.raises(CacheLimitExceeded):
        warehouse.fetch_issues(config(tmp_path, max_cache_issues=2))


def test_exact_row_and_utf8_byte_limits_then_one_byte_over(tmp_path):
    workspace = Workspace(tmp_path / "cache.sqlite")
    rows = [issue("one"), issue("two")]
    size = sum(len(issue_json(row).encode()) for row in rows)
    workspace.replace_synced_data(rows, {"tests": []}, max_issues=2, max_bytes=size)
    before = workspace.rows(), workspace.last_sync(), workspace.health_rows()
    with pytest.raises(CacheLimitExceeded):
        workspace.replace_synced_data(rows, {"tests": ["changed"]}, max_bytes=size - 1)
    assert (workspace.rows(), workspace.last_sync(), workspace.health_rows()) == before
    with pytest.raises(CacheLimitExceeded):
        workspace.replace_snapshot(rows + [issue("three")], max_issues=2)
    assert workspace.rows() == before[0]


@pytest.mark.parametrize("budget", ["rows", "bytes"])
def test_retained_pending_rows_count_toward_limit_without_losing_edits(tmp_path, budget):
    workspace = Workspace(tmp_path / "cache.sqlite")
    workspace.replace_synced_data([issue("pending")], {"tests": ["old"]})
    workspace.set_change("pending", "notes", "keep")
    before = workspace.rows(), workspace.health_rows(), workspace.last_sync(), workspace.pending()
    incoming = issue("new")
    limits = {"max_issues": 1} if budget == "rows" else {
        "max_bytes": len(issue_json(incoming).encode()) + 50,
    }
    with pytest.raises(CacheLimitExceeded):
        workspace.replace_synced_data([incoming], {"tests": ["new"]}, **limits)
    assert (workspace.rows(), workspace.health_rows(), workspace.last_sync(), workspace.pending()) == before


def test_snapshot_and_health_replacement_roll_back_together(tmp_path):
    workspace = Workspace(tmp_path / "cache.sqlite")
    workspace.replace_synced_data([issue("old")], {"tests": ["old"]})
    before = workspace.rows(), workspace.health_rows(), workspace.last_sync()
    with workspace.connect() as connection:
        connection.execute("create trigger refuse_health before insert on health_snapshot "
                           "begin select raise(abort, 'injected health failure'); end")
    with pytest.raises(Exception, match="injected health failure"):
        workspace.replace_synced_data([issue("new")], {"tests": ["new"]})
    assert (workspace.rows(), workspace.health_rows(), workspace.last_sync()) == before


def test_pending_overlay_text_is_bounded_before_decoding(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path / "cache.sqlite")
    workspace.replace_synced_data([issue("one")], {"tests": ["old"]})
    workspace.set_change("one", "notes", "x" * 1000)
    before = workspace.last_sync(), workspace.health_rows()
    with monkeypatch.context() as context:
        context.setattr("dbt_dqm_app.store.json.loads", MagicMock(side_effect=AssertionError("decoded")))
        with pytest.raises(CacheLimitExceeded):
            workspace.rows(max_bytes=1000)
        with pytest.raises(CacheLimitExceeded):
            workspace.replace_synced_data([issue("one")], {"tests": ["new"]}, max_bytes=1000)
    assert (workspace.last_sync(), workspace.health_rows()) == before


def test_oversized_old_cache_is_not_decoded_and_pending_pages_remain_accessible(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path / "cache.sqlite")
    workspace.replace_snapshot([issue(str(i)) for i in range(201)])
    for row in workspace.rows():
        workspace.set_change(row["occurrence_id"], "notes", "pending")
    monkeypatch.setattr("dbt_dqm_app.store.json.loads", MagicMock(side_effect=AssertionError("decoded")))
    with pytest.raises(CacheLimitExceeded):
        workspace.rows(max_issues=200)
    pages = [workspace.pending_page(i) for i in (1, 2, 3)]
    assert [len(page) for page in pages] == [100, 100, 1]
    assert len({p.occurrence_id for page in pages for p in page}) == 201
    assert workspace.drifted_patches() == []


def test_postgres_download_uses_named_bounded_cursor(tmp_path, monkeypatch):
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    connection.__enter__.return_value = connection
    cursor.fetchmany.side_effect = [[issue("one")], []]
    monkeypatch.setattr(warehouse, "_postgres_connection", lambda _: connection)
    assert list(warehouse._iter_issue_rows(config(tmp_path), "select issues")) == [issue("one")]
    assert connection.cursor.call_args.kwargs["name"].startswith("dqm_sync_")
    assert all(call.args == (500,) for call in cursor.fetchmany.call_args_list)


def test_bigquery_download_requests_bounded_pages(tmp_path, monkeypatch):
    client = MagicMock()
    client.query.return_value.result.return_value.pages = [[issue("one")], [issue("two")]]
    monkeypatch.setattr(warehouse, "client_for", lambda _: client)
    assert len(list(warehouse._iter_issue_rows(replace(config(tmp_path), adapter_type="bigquery"),
                                              "select issues"))) == 2
    client.query.return_value.result.assert_called_once_with(page_size=500)
