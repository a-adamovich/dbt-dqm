"""Credential-file overrides: only service-account and authorized-user files, validated before
any dbt subprocess or BigQuery client, with fixed error messages (#24 follow-up)."""

import json
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from dbt_dqm_app import warehouse
from dbt_dqm_app.config import AppConfig
from dbt_dqm_app.errors import AppError

SECRET = "dqm-secret-marker-7f3a"


def _config(tmp_path, credentials_file):
    return replace(
        AppConfig(tmp_path, tmp_path, "dev", "demo", "bigquery", "proj", "ds", "US", "oauth", None),
        credentials_file=credentials_file,
    )


def _write(tmp_path, content, name="override.json"):
    path = tmp_path / name
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return path


def test_authorized_user_file_builds_scoped_user_credentials(tmp_path):
    path = _write(
        tmp_path,
        {"type": "authorized_user", "client_id": "c", "client_secret": "s", "refresh_token": "r"},
    )
    credentials = warehouse.override_credentials(path)
    assert credentials.refresh_token == "r"
    assert credentials.scopes == warehouse.BIGQUERY_SCOPES


def test_service_account_file_uses_the_service_account_loader(tmp_path, monkeypatch):
    built = MagicMock(return_value="service-account-creds")
    monkeypatch.setattr(warehouse.service_account.Credentials, "from_service_account_info", built)
    path = _write(tmp_path, {"type": "service_account", "client_email": "runner@example.com"})
    assert warehouse.override_credentials(path) == "service-account-creds"
    assert built.call_args.args[0]["client_email"] == "runner@example.com"
    assert built.call_args.kwargs["scopes"] == warehouse.BIGQUERY_SCOPES


def test_the_file_is_parsed_once_per_version(tmp_path, monkeypatch):
    built = MagicMock(side_effect=["first", "second"])
    monkeypatch.setattr(warehouse.service_account.Credentials, "from_service_account_info", built)
    path = _write(tmp_path, {"type": "service_account", "client_email": "a"})
    assert warehouse.override_credentials(path) == warehouse.override_credentials(path) == "first"
    path.write_text(json.dumps({"type": "service_account", "client_email": "bb"}))
    assert warehouse.override_credentials(path) == "second"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ({"type": "external_account", "credential_source": {"executable": SECRET}},
         warehouse.UNSUPPORTED_CREDENTIALS),
        ({"type": "impersonated_service_account"}, warehouse.UNSUPPORTED_CREDENTIALS),
        ({"type": SECRET}, warehouse.UNSUPPORTED_CREDENTIALS),
        ({"type": 7}, warehouse.UNSUPPORTED_CREDENTIALS),
        ({"no_type": SECRET}, warehouse.UNSUPPORTED_CREDENTIALS),
        ([SECRET], warehouse.UNSUPPORTED_CREDENTIALS),
        ("{not json " + SECRET, warehouse.UNREADABLE_CREDENTIALS),
        ({"type": "authorized_user", "client_id": SECRET}, warehouse.INCOMPLETE_CREDENTIALS),
    ],
    ids=[
        "external-account", "impersonated", "unknown-type", "non-string-type", "missing-type",
        "not-an-object", "malformed-json", "incomplete",
    ],
)
def test_rejected_files_get_fixed_messages_without_file_contents(tmp_path, content, message):
    path = _write(tmp_path, content)
    with pytest.raises(AppError) as raised:
        warehouse.override_credentials(path)
    error = raised.value
    assert error.message == message
    assert error.details == ""
    # No loader exception (whose text could quote the file) is chained to the error.
    assert error.__cause__ is None and (error.__context__ is None or error.__suppress_context__)
    assert SECRET not in str(error) and SECRET not in error.message


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(AppError, match="can't be read"):
        warehouse.override_credentials(tmp_path / "missing.json")


@pytest.mark.parametrize("worker", ["sync", "apply"])
def test_rejected_override_makes_no_dbt_or_client_calls(tmp_path, monkeypatch, worker):
    run, client = MagicMock(), MagicMock()
    monkeypatch.setattr(warehouse.subprocess, "run", run)
    monkeypatch.setattr(warehouse.bigquery, "Client", client)
    path = _write(tmp_path, {"type": "external_account", "credential_source": {"file": SECRET}})
    config = _config(tmp_path, path)
    with pytest.raises(AppError) as raised:
        if worker == "sync":
            warehouse.sync_worker(config)
        else:
            warehouse.apply_worker(config, [])
    assert raised.value.message == warehouse.UNSUPPORTED_CREDENTIALS
    run.assert_not_called()
    client.assert_not_called()


def test_application_default_credentials_need_no_file(tmp_path, monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(warehouse.bigquery, "Client", client)
    warehouse.client_for(_config(tmp_path, None))
    assert "credentials" not in client.call_args.kwargs
