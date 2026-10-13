"""Credentials go only where they are bound: base_url's origin (or auth_hosts), and the
hosts listed for each secret in the protocol's `secret_hosts`."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pytest_httpserver import HTTPServer

from databolt.ingestion import api
from databolt.patterns import EgressPolicy, Registry, run
from databolt.patterns.errors import CrossOriginPagination, SecretHostMismatch, ValidationError
from databolt.patterns.net.credentials import CredentialPolicy, host_matches
from databolt.patterns.protocol import handle
from tests.api_helpers import run_rest

SECRET = "tok-very-secret-1"
KEY = "key-very-secret-2"
CREDENTIAL_HEADERS = ("Authorization", "X-Api-Key", "Cookie", "X-Token")


@pytest.fixture
def other() -> Iterator[HTTPServer]:
    """A second server: same host, different port, so a different origin."""
    server = HTTPServer(host="127.0.0.1", port=0)
    server.start()
    yield server
    server.clear()
    if server.is_running():
        server.stop()


def assert_no_credentials(server: HTTPServer) -> None:
    for req, _ in server.log:
        for h in CREDENTIAL_HEADERS:
            assert h not in req.headers, f"{h} leaked to {server.url_for('/')}"
        assert SECRET not in req.full_path and KEY not in req.full_path


def rest_params(base: HTTPServer, auth: Any, **client: Any) -> dict[str, Any]:
    return {
        "client": {"base_url": base.url_for("/"), "auth": auth, **client},
        "resource": {"path": "items", "data_selector": "items"},
    }


BEARER = {"type": "bearer"}
APIKEY_Q = {"type": "api_key", "name": "apikey", "location": "query"}
APIKEY_H = {"type": "api_key", "name": "X-Api-Key"}
BASIC = {"type": "http_basic", "username": "u"}
SECRETS = {"token": SECRET, "api_key": KEY, "password": SECRET}


# -- paginator next links ------------------------------------------------------------------


@pytest.mark.parametrize("auth", [BEARER, APIKEY_Q, APIKEY_H, BASIC])
def test_cross_origin_json_link_is_refused_by_default(
    httpserver: HTTPServer, other: HTTPServer, auth: Any
) -> None:
    httpserver.expect_request("/items").respond_with_json(
        {"items": [{"i": 1}], "next": other.url_for("/steal")}
    )
    other.expect_request("/steal").respond_with_json({"items": []})
    with pytest.raises(CrossOriginPagination) as ei:
        run_rest(rest_params(httpserver, auth), secrets=SECRETS)
    assert ei.value.code == "cross_origin_pagination"
    assert len(other.log) == 0


@pytest.mark.parametrize("auth", [BEARER, APIKEY_Q, APIKEY_H, BASIC])
def test_cross_origin_json_link_opt_in_follows_without_credentials(
    httpserver: HTTPServer, other: HTTPServer, auth: Any
) -> None:
    httpserver.expect_request("/items").respond_with_json(
        {"items": [{"i": 1}], "next": other.url_for("/page2")}
    )
    other.expect_request("/page2").respond_with_json({"items": [{"i": 2}]})
    _res, rows = run_rest(
        rest_params(httpserver, auth, allow_cross_origin_pagination=True), secrets=SECRETS
    )
    assert len(rows) == 2
    assert len(other.log) == 1
    assert_no_credentials(other)
    # same-origin first page did get credentials
    first = httpserver.log[0][0]
    assert (
        ("Authorization" in first.headers)
        or ("X-Api-Key" in first.headers)
        or (KEY in first.full_path)
    )


def test_cross_origin_header_link(httpserver: HTTPServer, other: HTTPServer) -> None:
    httpserver.expect_request("/items").respond_with_json(
        {"items": [{"i": 1}]}, headers={"Link": f'<{other.url_for("/p2")}>; rel="next"'}
    )
    other.expect_request("/p2").respond_with_json({"items": [{"i": 2}]})
    with pytest.raises(CrossOriginPagination):
        run_rest(rest_params(httpserver, BEARER, paginator="header_link"), secrets=SECRETS)
    assert len(other.log) == 0
    run_rest(
        rest_params(
            httpserver, BEARER, paginator="header_link", allow_cross_origin_pagination=True
        ),
        secrets=SECRETS,
    )
    assert_no_credentials(other)


def test_same_origin_pagination_keeps_credentials(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/items", query_string="").respond_with_json(
        {"items": [{"i": 1}], "next": "/items?p=2"}
    )
    httpserver.expect_request("/items", query_string="p=2").respond_with_json({"items": []})
    run_rest(rest_params(httpserver, BEARER), secrets=SECRETS)
    assert all(r.headers["Authorization"] == f"Bearer {SECRET}" for r, _ in httpserver.log)


def test_absolute_path_to_another_origin(httpserver: HTTPServer, other: HTTPServer) -> None:
    other.expect_request("/elsewhere").respond_with_json({"items": [{"i": 1}]})
    p = rest_params(httpserver, BEARER)
    p["resource"]["path"] = other.url_for("/elsewhere")
    with pytest.raises(CrossOriginPagination):
        run_rest(p, secrets=SECRETS)
    assert len(other.log) == 0
    p["client"]["allow_cross_origin_pagination"] = True
    _, rows = run_rest(p, secrets=SECRETS)
    assert rows == [{"i": 1}]
    assert_no_credentials(other)


# -- redirects (the client chokepoint) -------------------------------------------------------


def test_cross_origin_redirect_strips_every_credential(
    httpserver: HTTPServer, other: HTTPServer
) -> None:
    httpserver.expect_request("/items").respond_with_data(
        "", status=302, headers={"Location": other.url_for("/landing")}
    )
    other.expect_request("/landing").respond_with_json({"items": [{"i": 1}]})
    p = rest_params(httpserver, APIKEY_H)
    # a templated secret in a custom header is a credential too
    p["client"]["headers"] = {"X-Token": "${secrets.token}", "Cookie": "s=1"}
    res, rows = run_rest(p, secrets=SECRETS)
    assert rows == [{"i": 1}]
    assert_no_credentials(other)
    assert any("withheld secret" in w for w in res.warnings)


# -- secret_hosts (runner protocol) ----------------------------------------------------------


def proto(base: HTTPServer, tmp: Path, secret_hosts: Any, **extra: Any) -> dict[str, Any]:
    return {
        "protocol": 1,
        "op": "run",
        "pattern": {"id": "databolt.ingestion.api.rest", "version": "^1"},
        "params": rest_params(base, BEARER),
        "secrets": {"token": SECRET},
        "secret_hosts": secret_hosts,
        "egress": {"allow_private": True, "trust_env": False},
        "output": {"dir": str(tmp)},
        **extra,
    }


def test_secret_hosts_match(httpserver: HTTPServer, tmp_path: Path) -> None:
    httpserver.expect_request("/items").respond_with_json({"items": [{"i": 1}]})
    result, rc = handle(json.dumps(proto(httpserver, tmp_path, {"token": ["127.0.0.1"]})))
    assert rc == 0, result
    assert httpserver.log[0][0].headers["Authorization"] == f"Bearer {SECRET}"


@pytest.mark.parametrize(
    "hosts",
    [{"token": []}, {"token": ["api.github.com"]}, {"token": ["*.0.0.1"]}, {"other": ["x"]}, {}],
)
def test_secret_hosts_mismatch_fails_before_any_request(
    httpserver: HTTPServer, tmp_path: Path, hosts: Any
) -> None:
    httpserver.expect_request("/items").respond_with_json({"items": []})
    result, rc = handle(json.dumps(proto(httpserver, tmp_path, hosts)))
    assert rc == 2
    assert result["error"]["code"] == "secret_host_mismatch"
    assert "token" in result["error"]["message"] and "127.0.0.1" in result["error"]["message"]
    assert SECRET not in json.dumps(result)
    assert len(httpserver.log) == 0


def test_secret_hosts_on_validate(httpserver: HTTPServer, tmp_path: Path) -> None:
    req = proto(httpserver, tmp_path, {"token": ["api.example.com"]})
    req["op"] = "validate"
    result, rc = handle(json.dumps(req))
    assert rc == 2 and result["error"]["code"] == "secret_host_mismatch"


def test_secret_hosts_bind_paginated_requests_too(
    httpserver: HTTPServer, other: HTTPServer, tmp_path: Path
) -> None:
    """Even an allowed cross-origin page never receives a secret bound elsewhere."""
    httpserver.expect_request("/items").respond_with_json(
        {"items": [{"i": 1}], "next": other.url_for("/p2")}
    )
    other.expect_request("/p2").respond_with_json({"items": [{"i": 2}]})
    req = proto(httpserver, tmp_path, {"token": ["127.0.0.1"]})
    req["params"]["client"]["allow_cross_origin_pagination"] = True
    result, rc = handle(json.dumps(req))
    assert rc == 0, result
    assert result["rows"] == 2
    assert_no_credentials(other)


def test_studio_request_shape_with_secret_hosts(httpserver: HTTPServer, tmp_path: Path) -> None:
    httpserver.expect_request("/items").respond_with_json({"items": [{"i": 1}]})
    req = proto(
        httpserver,
        tmp_path,
        {"token": ["127.0.0.1"]},
        run_id="run-9",
        state=None,
        limits={"total_timeout_s": 110, "max_response_bytes": 26214400},
    )
    req["egress"]["unwrap_ipv4"] = ["::ffff:0:0/96", "::/96", "64:ff9b::/96"]
    result, rc = handle(json.dumps(req))
    assert rc == 0, result
    assert result["warnings"] == []


@pytest.mark.parametrize(
    ("host", "patterns", "ok"),
    [
        ("api.github.com", ["api.github.com"], True),
        ("API.GitHub.com", ["api.github.com"], True),
        ("github.com", ["api.github.com"], False),
        ("a.example.com", ["*.example.com"], True),
        ("a.b.example.com", ["*.example.com"], True),
        ("example.com", ["*.example.com"], False),
        ("evilexample.com", ["*.example.com"], False),
        ("example.com.evil.io", ["example.com"], False),
        ("x", [], False),
        ("127.0.0.1", ["*.0.0.1"], False),
        ("127.0.0.1", ["127.0.0.1"], True),
    ],
)
def test_host_matches(host: str, patterns: list[str], ok: bool) -> None:
    assert host_matches(host, patterns) is ok


def test_https_to_http_downgrade_denied() -> None:
    policy = CredentialPolicy()
    policy.bind_origin("https://api.example.com/v1/")
    assert policy.denial("token", "https://api.example.com/v1/x") is None
    assert "downgrade" in (policy.denial("token", "http://api.example.com/v1/x") or "")
    assert policy.denial("token", "https://other.example.com/") is not None
    policy.bind_origin("https://api.example.com/", auth_hosts=["*.example.com"])
    assert policy.denial("token", "https://auth.example.com/token") is None


# -- OAuth token endpoint ------------------------------------------------------------------


def oauth(base: HTTPServer, token_server: HTTPServer, **client: Any) -> dict[str, Any]:
    auth = {
        "type": "oauth2_client_credentials",
        "token_url": token_server.url_for("/token"),
        "client_id": "cid",
    }
    return rest_params(base, auth, **client)


def test_oauth_token_url_off_origin_is_rejected(httpserver: HTTPServer, other: HTTPServer) -> None:
    with pytest.raises(ValidationError) as ei:
        run_rest(oauth(httpserver, other), secrets={"client_secret": SECRET})
    assert ei.value.code == "token_url_not_allowed"
    assert len(other.log) == 0


def test_oauth_token_url_allowlisted(httpserver: HTTPServer, other: HTTPServer) -> None:
    other.expect_request("/token", method="POST").respond_with_json({"access_token": "at-12345"})
    httpserver.expect_request(
        "/items", headers={"Authorization": "Bearer at-12345"}
    ).respond_with_json({"items": [{"i": 1}]})
    _, rows = run_rest(
        oauth(httpserver, other, auth_hosts=["127.0.0.1"]), secrets={"client_secret": SECRET}
    )
    assert rows == [{"i": 1}]


def test_oauth_secret_hosts_must_cover_token_host(
    httpserver: HTTPServer, other: HTTPServer, tmp_path: Path
) -> None:
    req = proto(httpserver, tmp_path, {"client_secret": ["api.example.com"]})
    req["params"] = oauth(httpserver, other, auth_hosts=["127.0.0.1"])
    req["secrets"] = {"client_secret": SECRET}
    result, rc = handle(json.dumps(req))
    assert rc == 2 and result["error"]["code"] == "secret_host_mismatch"
    assert "token_url" in result["error"]["message"]
    assert len(other.log) == 0


# -- custom code through ctx.http ------------------------------------------------------------


def test_ctx_http_withholds_secrets_from_unbound_hosts(httpserver: HTTPServer) -> None:
    from databolt.patterns import RunContext, pattern

    @pattern(id="t.leaky", version="1.0.0", secrets=["token"])
    def leaky(params: Any, ctx: RunContext) -> Iterator[dict[str, Any]]:
        resp = ctx.http.get(httpserver.url_for("/x"), headers={"X-Token": ctx.secrets["token"]})
        yield resp.json()

    httpserver.expect_request("/x").respond_with_json({"ok": 1})
    res = run(
        Registry().register(leaky),
        {},
        secrets={"token": SECRET},
        secret_hosts={"token": ["api.example.com"]},
        egress=EgressPolicy(allow_private=True, trust_env=False),
    )
    assert "X-Token" not in httpserver.log[0][0].headers
    assert any("withheld secret 'token'" in w for w in res.warnings)


def test_ctx_http_refuses_secret_in_path(httpserver: HTTPServer) -> None:
    from databolt.patterns import RunContext, pattern
    from databolt.patterns.errors import CredentialBlocked

    @pattern(id="t.pathy", version="1.0.0", secrets=["token"])
    def pathy(params: Any, ctx: RunContext) -> Iterator[dict[str, Any]]:
        yield ctx.http.get(httpserver.url_for(f"/k/{ctx.secrets['token']}")).json()

    with pytest.raises(CredentialBlocked):
        run(
            Registry().register(pathy),
            {},
            secrets={"token": SECRET},
            secret_hosts={"token": []},
            egress=EgressPolicy(allow_private=True, trust_env=False),
        )
    assert len(httpserver.log) == 0


def test_http_get_secret_hosts(httpserver: HTTPServer) -> None:
    src = api.http_get(
        httpserver.url_for("/d.json"),
        auth=api.auth.bearer(),
        secrets={"token": SECRET},
        egress=EgressPolicy(allow_private=True, trust_env=False),
    )
    src.pattern  # noqa: B018
    with pytest.raises(SecretHostMismatch):
        run(
            src.pattern,
            src.params,
            secrets={"token": SECRET},
            secret_hosts={"token": ["example.com"]},
            egress=EgressPolicy(allow_private=True, trust_env=False),
        )
