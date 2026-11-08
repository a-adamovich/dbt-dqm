"""Tests for optional validation usage measurement."""

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 7, 20, tzinfo=UTC)


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return importlib.import_module("query_metrics"), None


def test_cost_metrics_exclude_parent_scripts_and_report_unlabelled_queries(modules, monkeypatch):
    from types import SimpleNamespace

    from google.cloud import bigquery

    local, _ = modules
    queries = []
    def query(sql, job_config):
        queries.append((sql, job_config))
        return SimpleNamespace(result=lambda: [{"category": "unlabelled_app_or_other_activity",
                                               "jobs": 3, "billed_bytes": 100, "slot_ms": 12}])
    monkeypatch.setattr(bigquery, "Client", lambda **kwargs: SimpleNamespace(query=query))
    result = local.job_metrics("private-project", "opaque-run", NOW - timedelta(hours=1), NOW)
    assert result["status"] == "measured"
    assert result["categories"][0]["billed_bytes"] == 100
    assert "statement_type is distinct from 'SCRIPT'" in queries[0][0]
    assert "parent_job_id in" in queries[0][0]
    assert queries[0][1].maximum_bytes_billed == 1024 ** 3


def test_denied_metrics_are_unavailable_instead_of_zero(modules, monkeypatch):
    from google.api_core.exceptions import Forbidden
    from google.cloud import bigquery

    local, _ = modules
    def denied(**kwargs):
        raise Forbidden("private project or account details")
    monkeypatch.setattr(bigquery, "Client", denied)
    result = local.job_metrics("private-project", "opaque-run", NOW, NOW)
    assert result == {"status": "unavailable", "reason": "Forbidden"}


