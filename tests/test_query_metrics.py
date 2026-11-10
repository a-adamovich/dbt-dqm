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


def test_dataset_attribution_avoids_a_correlated_subquery(modules, monkeypatch):
    metrics, _ = modules
    queries = view_clients(monkeypatch, {'JOBS_BY_PROJECT': [{'category': 'labelled', 'jobs': 1}]})
    metrics.job_metrics('private-project', 'opaque-run', NOW, NOW)
    # BigQuery rejected this form with invalidQuery, so spend was never measured.
    assert 'exists(select 1 from unnest(referenced_tables)' not in queries[0]
    assert 'job_id in (select job_id from touched)' in queries[0]
    assert "r.dataset_id='dbt_dqm_qa'" in queries[0]


def test_denied_metrics_are_unavailable_instead_of_zero(modules, monkeypatch):
    from google.api_core.exceptions import Forbidden
    from google.cloud import bigquery

    local, _ = modules
    def denied(**kwargs):
        raise Forbidden("private project or account details")
    monkeypatch.setattr(bigquery, "Client", denied)
    result = local.job_metrics("private-project", "opaque-run", NOW, NOW)
    assert result == {"status": "unavailable", "reason": "Forbidden"}




def test_missing_statistics_are_not_reported_as_zero(modules, monkeypatch):
    from types import SimpleNamespace

    from google.cloud import bigquery

    metrics, _ = modules
    queries = []
    def query(sql, job_config):
        queries.append(sql)
        return SimpleNamespace(result=lambda: [{'category': 'labelled', 'jobs': 2,
            'billed_bytes': None, 'slot_ms': None, 'jobs_missing_billed_bytes': 2,
            'jobs_missing_slot_ms': 2}])
    monkeypatch.setattr(bigquery, 'Client', lambda **kwargs: SimpleNamespace(query=query))
    result = metrics.job_metrics('private-project', 'opaque-run', NOW, NOW)
    assert result['categories'][0]['billed_bytes'] is None
    assert 'countif(total_bytes_billed is null)' in queries[0]
    assert 'coalesce(total_bytes_billed,0)' not in queries[0]


def test_empty_attribution_is_unavailable(modules, monkeypatch):
    from types import SimpleNamespace

    from google.cloud import bigquery

    metrics, _ = modules
    monkeypatch.setattr(bigquery, 'Client', lambda **kwargs: SimpleNamespace(
        query=lambda *args, **kwargs: SimpleNamespace(result=list)))
    assert metrics.job_metrics('private-project', 'opaque-run', NOW, NOW)['status'] == 'unavailable'


def view_clients(monkeypatch, answers):
    """Route each query to an answer chosen by the metadata view it reads."""
    from types import SimpleNamespace

    from google.cloud import bigquery

    queries = []
    def query(sql, job_config):
        queries.append(sql)
        view = next(name for name in answers if name in sql)
        def result():
            if isinstance(answers[view], Exception):
                raise answers[view]
            return answers[view]
        return SimpleNamespace(result=result)
    monkeypatch.setattr(bigquery, 'Client', lambda **kwargs: SimpleNamespace(query=query))
    return queries


def test_project_view_reports_its_scope(modules, monkeypatch):
    metrics, _ = modules
    queries = view_clients(monkeypatch, {'JOBS_BY_PROJECT': [{'category': 'labelled', 'jobs': 1}]})
    result = metrics.job_metrics('private-project', 'opaque-run', NOW, NOW)
    assert result['scope'] == 'project'
    assert len(queries) == 1


def test_denied_project_view_falls_back_to_the_querying_identity(modules, monkeypatch):
    from google.api_core.exceptions import Forbidden

    metrics, _ = modules
    queries = view_clients(monkeypatch, {'JOBS_BY_PROJECT': Forbidden('private details'),
                                         'JOBS_BY_USER': [{'category': 'labelled', 'jobs': 4}]})
    result = metrics.job_metrics('private-project', 'opaque-run', NOW, NOW)
    assert result['status'] == 'measured' and result['scope'] == 'querying_identity'
    assert 'other identities' in result['scope_note']
    assert len(queries) == 2


def test_both_views_denied_is_unavailable_instead_of_zero(modules, monkeypatch):
    from google.api_core.exceptions import Forbidden

    metrics, _ = modules
    view_clients(monkeypatch, {'JOBS_BY_PROJECT': Forbidden('a'), 'JOBS_BY_USER': Forbidden('b')})
    assert metrics.job_metrics('private-project', 'opaque-run', NOW, NOW) == {
        'status': 'unavailable', 'reason': 'Forbidden'}


def test_other_errors_do_not_fall_back(modules, monkeypatch):
    metrics, _ = modules
    queries = view_clients(monkeypatch, {'JOBS_BY_PROJECT': ValueError('bad'),
                                         'JOBS_BY_USER': [{'category': 'labelled', 'jobs': 4}]})
    assert metrics.job_metrics('private-project', 'opaque-run', NOW, NOW) == {
        'status': 'unavailable', 'reason': 'ValueError'}
    assert len(queries) == 1


def test_failure_keeps_the_structured_reason_but_never_the_message(monkeypatch):
    from google.api_core.exceptions import BadRequest
    metrics = importlib.import_module('query_metrics')
    error = BadRequest('private-project was mentioned here',
                       errors=[{'reason': 'invalidQuery', 'message': 'private-project was mentioned here'}])
    view_clients(monkeypatch, {'JOBS_BY_PROJECT': error, 'JOBS_BY_USER': error})
    result = metrics.job_metrics('private-project', 'opaque-run', NOW, NOW)
    assert result == {'status': 'unavailable', 'reason': 'BadRequest', 'view': 'JOBS_BY_PROJECT',
                      'error_code': 'invalidQuery'}
    assert 'private-project' not in str(result)
