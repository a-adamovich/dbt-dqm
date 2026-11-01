"""Offline WIF compatibility through real app/dbt ADC loaders; no cloud setup or keys."""

import json
import sys
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import pytest
from dbt.adapters.bigquery import credentials as dbt_credentials
from google.auth import identity_pool
from google.auth.exceptions import GoogleAuthError

from dbt_dqm_app.config import AppConfig
from dbt_dqm_app.warehouse import client_for, validate_dbt

PROJECT = "offline-wif-project"
ACCOUNT = f"reviewer@{PROJECT}.iam.gserviceaccount.com"
STS = "https://sts.googleapis.com/v1/token"
IMPERSONATION = (
    f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{ACCOUNT}"
    ":generateAccessToken"
)


@pytest.fixture
def wif(tmp_path, monkeypatch):
    def unexpected_network(*args, **kwargs):
        pytest.fail("Offline WIF smoke attempted an unexpected network request")

    monkeypatch.setattr("requests.sessions.Session.request", unexpected_network)
    token = tmp_path / "subject-token"
    token.write_text("synthetic-external-subject-token")
    adc = tmp_path / "wif-adc.json"
    adc.write_text(json.dumps({
        "type": "external_account",
        "audience": "//iam.googleapis.com/projects/123/locations/global/"
                    "workloadIdentityPools/offline/providers/offline",
        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
        "token_url": STS,
        "service_account_impersonation_url": IMPERSONATION,
        "credential_source": {"file": str(token)},
    }))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(adc))
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", PROJECT)
    dbt_credentials._create_bigquery_defaults.cache_clear()
    config = AppConfig(tmp_path, tmp_path, "dev", "consumer", "bigquery",
                       PROJECT, "offline_dataset", "US", "oauth", None)
    yield config, adc
    dbt_credentials._create_bigquery_defaults.cache_clear()


def _credentials(loader, config):
    if loader == "app":
        return client_for(config)._credentials
    return dbt_credentials.create_google_credentials(dbt_credentials.BigQueryCredentials(
        method=dbt_credentials.BigQueryConnectionMethod.OAUTH,
        database=PROJECT, schema="offline_dataset",
    ))


class Response:
    def __init__(self, data, status=200):
        self.status = status
        self.data = json.dumps(data).encode()
        self.headers = {"content-type": "application/json"}


@pytest.mark.parametrize("loader", ["app", "dbt"])
def test_wif_adc_exchanges_impersonates_and_refreshes(wif, loader):
    config, _ = wif
    credentials = _credentials(loader, config)
    assert isinstance(credentials, identity_pool.Credentials)
    assert credentials.service_account_email == ACCOUNT
    calls = []

    def exchange(url, method="GET", body=None, headers=None, **kwargs):
        calls.append(url)
        assert method == "POST"
        if url == STS:
            values = parse_qs(body.decode())
            assert values["subject_token"] == ["synthetic-external-subject-token"]
            assert values["subject_token_type"] == ["urn:ietf:params:oauth:token-type:jwt"]
            return Response({"access_token": "federated-access-token", "expires_in": 3600,
                             "token_type": "Bearer"})
        assert url == IMPERSONATION, f"Unexpected endpoint: {url}"
        assert headers["authorization"] == "Bearer federated-access-token"
        payload = json.loads(body)
        assert "https://www.googleapis.com/auth/cloud-platform" in payload["scope"]
        return Response({
            "accessToken": f"impersonated-service-token-{calls.count(IMPERSONATION)}",
            "expireTime": (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })

    credentials.refresh(exchange)
    assert credentials.token == "impersonated-service-token-1" and credentials.valid
    headers = {}
    # apply verifies bearer headers without starting google-auth's optional background
    # regional-access lookup; only the token exchange/refresh is under test here.
    credentials.apply(headers)
    assert headers["authorization"] == "Bearer impersonated-service-token-1"
    assert calls == [STS, IMPERSONATION]
    credentials.refresh(exchange)
    assert calls[-1] == IMPERSONATION and calls.count(IMPERSONATION) == 2
    assert credentials.token == "impersonated-service-token-2"


@pytest.mark.parametrize("loader", ["app", "dbt"])
@pytest.mark.parametrize("failed_endpoint", [STS, IMPERSONATION])
def test_wif_authentication_failure_has_no_fallback(wif, loader, failed_endpoint):
    credentials = _credentials(loader, wif[0])
    calls = []

    def denied(url, method="GET", **kwargs):
        calls.append(url)
        assert url in (STS, IMPERSONATION) and method == "POST"
        if url == STS and failed_endpoint == IMPERSONATION:
            return Response({"access_token": "federated-access-token", "expires_in": 3600,
                             "token_type": "Bearer"})
        return Response({"error": "invalid_grant", "error_description": "Synthetic denial"}, 400)

    with pytest.raises(GoogleAuthError):
        credentials.refresh(denied)
    assert calls == ([STS] if failed_endpoint == STS else [STS, IMPERSONATION])
    assert credentials.token is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX script fixture; native ADC propagation has portability coverage")
def test_wif_adc_environment_reaches_dbt_subprocesses(wif, tmp_path, monkeypatch, fake_dbt_process):
    config, adc = wif
    binary = tmp_path / "bin"
    binary.mkdir()
    log = tmp_path / "dbt-environment.jsonl"
    executable = binary / "dbt"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(log)!r}, 'a') as output:\n"
        "    output.write(json.dumps({'command': sys.argv[1], "
        "'adc': os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')}) + '\\n')\n"
        "if sys.argv[1] == 'parse':\n"
        "    from pathlib import Path\n"
        "    target = Path(sys.argv[sys.argv.index('--target-path') + 1])\n"
        "    target.mkdir(parents=True, exist_ok=True)\n"
        "    (target / 'manifest.json').write_text(json.dumps({'metadata': {'invocation_id': 'test'}, 'nodes': {}, 'sources': {}}))\n"
    )
    executable.chmod(0o700)
    monkeypatch.setattr(sys, "executable", str(binary / "python"))
    from dbt_dqm_app import warehouse
    monkeypatch.setattr(warehouse.DbtCLI, "validate", lambda *args: None)
    monkeypatch.setattr(warehouse, "check_required_relations", lambda config: None)
    validate_dbt(config)
    assert [json.loads(line) for line in log.read_text().splitlines()] == [
        {"command": command, "adc": str(adc)} for command in ("debug", "parse")
    ]
