from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from google.cloud import bigquery

from dbt_dqm_app import warehouse
from dbt_dqm_app.config import AppConfig
from dbt_dqm_app.store import Patch


def config(tmp_path):
    return AppConfig(tmp_path, tmp_path, "dev", "demo", "bigquery", "db", "schema", "US",
                     "oauth", None)


def patch(**kwargs):
    return replace(Patch("one", "notes", None, "pending", 1, "2026-10-09T00:00:00Z", 0), **kwargs)


def client(monkeypatch):
    fake = MagicMock()
    fake.get_table.return_value.schema = [bigquery.SchemaField("upload_id", "STRING"),
                                         bigquery.SchemaField("staged_at", "TIMESTAMP")]
    monkeypatch.setattr(warehouse, "client_for", lambda _: fake)
    return fake


def test_duplicate_patches_are_canonical_and_conflicts_never_upload(tmp_path, monkeypatch):
    fake = client(monkeypatch)
    warehouse.apply_patches(config(tmp_path), [patch(), patch()])
    assert len(fake.load_table_from_json.call_args.args[0]) == 1
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        warehouse.apply_patches(config(tmp_path), [patch(), patch(new_value="different")])
    with pytest.raises(ValueError, match="Inconsistent"):
        warehouse.apply_patches(config(tmp_path), [patch(), patch(field_name="ticket_url",
                                                                 base_annotation_version=1)])
    assert fake.load_table_from_json.call_count == 1


def test_retries_have_same_batch_and_independent_uploads(tmp_path, monkeypatch):
    fake = client(monkeypatch)
    batches = [warehouse.apply_patches(config(tmp_path), [patch()]) for _ in range(2)]
    assert batches[0] == batches[1]
    payloads = [call.args[0][0] for call in fake.load_table_from_json.call_args_list]
    assert payloads[0]["upload_id"] != payloads[1]["upload_id"]
    assert all("staged_at" not in payload for payload in payloads)
    assert all(call.kwargs["job_id"].endswith(payload["upload_id"])
               for call, payload in zip(fake.load_table_from_json.call_args_list, payloads, strict=True))
    assert fake.query.call_count == 2  # No pre-transaction audit check.
    sql = fake.query.call_args.args[0]
    assert sql.index("begin transaction") < sql.index("set audited_count=")
    assert "if audited_count>0 then" in sql
    assert sql.index("else") < sql.index("Annotation upload expired")
    assert "any_value" not in sql
    assert "where upload_id=@upload_id and batch_id=@batch_id" in sql
    assert "interval 24 hour" in sql


@pytest.mark.parametrize("failure", ["load", "apply"])
def test_uncertain_failures_do_not_clean_staging(tmp_path, monkeypatch, failure):
    fake = client(monkeypatch)
    operation = fake.load_table_from_json if failure == "load" else fake.query
    operation.return_value.result.side_effect = RuntimeError("lost response")
    with pytest.raises(RuntimeError, match="lost response"):
        warehouse.apply_patches(config(tmp_path), [patch()])
    assert fake.query.call_count == (0 if failure == "load" else 1)
    if failure == "apply":
        sql = fake.query.call_args.args[0]
        assert sql.index("delete from") < sql.index("commit transaction")


def test_old_staging_schema_requires_migration_before_upload(tmp_path, monkeypatch):
    fake = client(monkeypatch)
    fake.get_table.return_value.schema = []
    with pytest.raises(Exception, match="migration 0004"):
        warehouse.apply_patches(config(tmp_path), [patch()])
    fake.load_table_from_json.assert_not_called()
