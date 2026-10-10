"""Exercise the OAuth code path that BigQuery tooling reaches through oauthlib.

dbt-dqm never calls these libraries itself: oauthlib is reached transitively through
`pandas-gbq` / `pydata-google-auth` -> `google-auth-oauthlib` -> `requests-oauthlib`. Service
account and application-default-credential connections don't touch it, so a warehouse
connection check can't prove oauthlib compatibility. These tests run the authorization URL,
token exchange and credential conversion against a mocked token endpoint.
"""

import json
from urllib.parse import parse_qs, urlparse

import requests
from google_auth_oauthlib import flow as oauth_flow
from google_auth_oauthlib import helpers

CLIENT_CONFIG = {
    "installed": {
        "client_id": "dqm-test-client.apps.googleusercontent.com",
        "client_secret": "dqm-test-secret",
        "auth_uri": "https://accounts.example.test/o/oauth2/auth",
        "token_uri": "https://oauth2.example.test/token",
        "redirect_uris": ["http://localhost"],
    }
}
SCOPES = ["https://www.googleapis.com/auth/bigquery"]


class TokenEndpoint(requests.adapters.BaseAdapter):
    """Answers the token request and records what the client sent."""

    def __init__(self):
        super().__init__()
        self.requests = []

    def send(self, request, **kwargs):
        self.requests.append(request)
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(
            {
                "access_token": "dqm-access-token",
                "refresh_token": "dqm-refresh-token",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": " ".join(SCOPES),
            }
        ).encode()
        response.url = request.url
        response.request = request
        return response

    def close(self):
        pass


def test_authorization_url_carries_client_scope_and_pkce():
    flow = oauth_flow.Flow.from_client_config(
        CLIENT_CONFIG, scopes=SCOPES, redirect_uri="http://localhost"
    )
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    query = parse_qs(urlparse(url).query)
    assert url.startswith(CLIENT_CONFIG["installed"]["auth_uri"])
    assert query["client_id"] == [CLIENT_CONFIG["installed"]["client_id"]]
    assert query["scope"] == [" ".join(SCOPES)]
    assert query["state"] == [state]
    assert query["code_challenge_method"] == ["S256"]


def test_token_exchange_and_credential_conversion():
    flow = oauth_flow.Flow.from_client_config(
        CLIENT_CONFIG, scopes=SCOPES, redirect_uri="http://localhost"
    )
    flow.authorization_url()
    endpoint = TokenEndpoint()
    flow.oauth2session.mount("https://", endpoint)
    flow.fetch_token(code="dqm-authorization-code")

    sent = parse_qs(endpoint.requests[0].body)
    assert endpoint.requests[0].url == CLIENT_CONFIG["installed"]["token_uri"]
    assert sent["grant_type"] == ["authorization_code"]
    assert sent["code"] == ["dqm-authorization-code"]
    assert "code_verifier" in sent
    credentials = helpers.credentials_from_session(flow.oauth2session, CLIENT_CONFIG["installed"])
    assert credentials.token == "dqm-access-token"
    assert credentials.refresh_token == "dqm-refresh-token"
    assert credentials.client_id == CLIENT_CONFIG["installed"]["client_id"]
    assert credentials.token_uri == CLIENT_CONFIG["installed"]["token_uri"]
