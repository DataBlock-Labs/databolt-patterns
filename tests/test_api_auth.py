from __future__ import annotations

import base64
import json

import pytest
from pytest_httpserver import HTTPServer

from databolt.ingestion import api
from databolt.patterns.errors import ValidationError
from databolt.patterns.protocol import handle
from tests.api_helpers import run_rest


def params(server: HTTPServer, auth: object, path: str = "data") -> dict[str, object]:
    return {"client": {"base_url": server.url_for("/"), "auth": auth}, "resource": {"path": path}}


def test_no_auth_sends_no_authorization(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/data").respond_with_json([{"a": 1}])
    run_rest(params(httpserver, "none"))
    assert "Authorization" not in httpserver.log[0][0].headers


def test_bearer(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/data", headers={"Authorization": "Bearer tok-1"}).respond_with_json(
        [{"a": 1}]
    )
    _res, rows = run_rest(params(httpserver, {"type": "bearer"}), secrets={"token": "tok-1"})
    assert rows == [{"a": 1}]


def test_bearer_custom_secret_name(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/data", headers={"Authorization": "Bearer gh"}).respond_with_json([])
    run_rest(
        params(httpserver, {"type": "bearer", "token": "${secrets.github_token}"}),
        secrets={"github_token": "gh"},
    )


def test_api_key_header_and_query(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/data", headers={"X-Key": "k-123"}).respond_with_json([])
    run_rest(params(httpserver, {"type": "api_key", "name": "X-Key"}), secrets={"api_key": "k-123"})
    httpserver.clear()
    httpserver.expect_request("/data", query_string="appid=k-456").respond_with_json([])
    run_rest(
        params(httpserver, {"type": "api_key", "name": "appid", "location": "query"}),
        secrets={"api_key": "k-456"},
    )


def test_http_basic(httpserver: HTTPServer) -> None:
    expected = "Basic " + base64.b64encode(b"alice:pw-secret").decode()
    httpserver.expect_request("/data", headers={"Authorization": expected}).respond_with_json([])
    run_rest(
        params(httpserver, {"type": "http_basic", "username": "alice"}),
        secrets={"password": "pw-secret"},
    )


@pytest.mark.parametrize("client_auth", ["basic", "body"])
def test_oauth2_client_credentials(httpserver: HTTPServer, client_auth: str) -> None:
    if client_auth == "basic":
        basic = "Basic " + base64.b64encode(b"cid:csecret").decode()
        httpserver.expect_request(
            "/token",
            method="POST",
            headers={"Authorization": basic},
            data="grant_type=client_credentials&scope=read",
        ).respond_with_json({"access_token": "at-999", "expires_in": 3600})
    else:
        httpserver.expect_request(
            "/token",
            method="POST",
            data="grant_type=client_credentials&scope=read&client_id=cid&client_secret=csecret",
        ).respond_with_json({"access_token": "at-999"})
    httpserver.expect_request(
        "/data", headers={"Authorization": "Bearer at-999"}
    ).respond_with_json({"items": [{"i": 1}], "next": "/data?p=2"})
    httpserver.expect_request(
        "/data", query_string="p=2", headers={"Authorization": "Bearer at-999"}
    ).respond_with_json({"items": [{"i": 2}]})
    auth = {
        "type": "oauth2_client_credentials",
        "token_url": httpserver.url_for("/token"),
        "client_id": "cid",
        "scope": "read",
        "client_auth": client_auth,
    }
    p = params(httpserver, auth)
    p["resource"] = {"path": "data", "data_selector": "items"}  # type: ignore[index]
    _res, rows = run_rest(p, secrets={"client_secret": "csecret"})
    assert len(rows) == 2
    token_calls = [r for r, _ in httpserver.log if r.path == "/token"]
    assert len(token_calls) == 1  # cached for the run


def test_literal_secret_in_auth_is_rejected() -> None:
    with pytest.raises(ValidationError, match="token"):
        api.RestApiPattern().parse_params(
            {"client": {"base_url": "https://x.test", "auth": {"type": "bearer", "token": "abc"}}}
        )


def test_missing_secret_is_reported_before_any_request(httpserver: HTTPServer) -> None:
    with pytest.raises(ValidationError, match="missing secret"):
        run_rest(params(httpserver, {"type": "bearer"}))
    assert len(httpserver.log) == 0


def test_secret_never_in_error_or_logs(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/data").respond_with_data("token sk-abc-123 is invalid", status=401)
    request = {
        "protocol": 1,
        "op": "run",
        "pattern": {"id": "databolt.ingestion.api.rest"},
        "params": params(httpserver, {"type": "bearer"}),
        "secrets": {"token": "sk-abc-123"},
        "egress": {"allow_private": True, "trust_env": False},
        "output": {"dir": "unused"},
    }
    result, rc = handle(json.dumps(request))
    assert rc == 3
    assert result["error"]["http_status"] == 401
    assert result["error"]["retryable"] is False
    assert "sk-abc-123" not in json.dumps(result)


def test_auth_helpers() -> None:
    assert api.auth.bearer().token == "${secrets.token}"
    assert api.auth.api_key(location="query").location == "query"
    assert api.auth.http_basic("u").username == "u"
    assert api.auth.oauth2_client_credentials("https://t", "id").client_auth == "basic"
    assert api.auth.none().type == "none"
