from __future__ import annotations

import socket
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from databolt.patterns.errors import (
    BudgetExhausted,
    EgressBlocked,
    HttpError,
    ResponseTooLarge,
    TransportError,
)
from databolt.patterns.limits import Budget, Limits
from databolt.patterns.net.backoff import full_jitter, is_retryable_status, parse_retry_after
from databolt.patterns.net.client import merge_params, parse_link_header
from databolt.patterns.net.ratelimit import RateLimiter


def test_get_json(httpserver: HTTPServer, make_client) -> None:
    httpserver.expect_request("/items", query_string="a=1&b=true").respond_with_json([1, 2])
    resp = make_client().get(httpserver.url_for("/items"), params={"a": 1, "b": True})
    assert resp.ok and resp.json() == [1, 2]


def test_429_retry_after_seconds(httpserver: HTTPServer, make_client, sleeps) -> None:
    httpserver.expect_ordered_request("/").respond_with_data(
        "slow down", status=429, headers={"Retry-After": "2"}
    )
    httpserver.expect_ordered_request("/").respond_with_json({"ok": True})
    client = make_client()
    assert client.get(httpserver.url_for("/")).json() == {"ok": True}
    assert sleeps == [2.0]
    assert client.stats.retries == 1
    assert client.stats.requests == 2


def test_429_retry_after_http_date(httpserver: HTTPServer, make_client, sleeps) -> None:
    when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=5), usegmt=True)
    httpserver.expect_ordered_request("/").respond_with_data(
        "", status=429, headers={"Retry-After": when}
    )
    httpserver.expect_ordered_request("/").respond_with_json([])
    make_client().get(httpserver.url_for("/"))
    assert len(sleeps) == 1
    assert 3.0 <= sleeps[0] <= 5.0


def test_5xx_then_success(httpserver: HTTPServer, make_client, sleeps) -> None:
    httpserver.expect_ordered_request("/").respond_with_data("oops", status=503)
    httpserver.expect_ordered_request("/").respond_with_data("oops", status=502)
    httpserver.expect_ordered_request("/").respond_with_json({"ok": 1})
    client = make_client(backoff_base_s=0.5, backoff_max_s=30)
    assert client.get(httpserver.url_for("/")).json() == {"ok": 1}
    assert len(sleeps) == 2
    assert 0 <= sleeps[0] <= 0.5 and 0 <= sleeps[1] <= 1.0  # full jitter bounds
    assert client.stats.retries == 2


def test_retries_exhausted(httpserver: HTTPServer, make_client, sleeps) -> None:
    httpserver.expect_request("/").respond_with_data("down", status=500)
    client = make_client(max_retries=2)
    with pytest.raises(HttpError) as ei:
        client.get(httpserver.url_for("/"))
    assert ei.value.http_status == 500
    assert ei.value.retryable is True
    assert "after 3 attempts" in ei.value.message
    assert len(httpserver.log) == 3
    assert len(sleeps) == 2


def test_4xx_is_not_retried(httpserver: HTTPServer, make_client, sleeps) -> None:
    httpserver.expect_request("/").respond_with_data("nope", status=404)
    with pytest.raises(HttpError) as ei:
        make_client().get(httpserver.url_for("/"))
    assert ei.value.http_status == 404 and ei.value.retryable is False
    assert len(httpserver.log) == 1 and sleeps == []


def test_allow_statuses_returns_instead_of_raising(httpserver: HTTPServer, make_client) -> None:
    httpserver.expect_request("/").respond_with_data("", status=404)
    assert make_client().get(httpserver.url_for("/"), allow_statuses=[404]).status_code == 404


def test_retry_after_longer_than_limit_fails_fast(
    httpserver: HTTPServer, make_client, sleeps
) -> None:
    httpserver.expect_request("/").respond_with_data(
        "", status=429, headers={"Retry-After": "3600"}
    )
    with pytest.raises(HttpError, match="retry after 3600s") as ei:
        make_client(max_retry_after_s=60).get(httpserver.url_for("/"))
    assert ei.value.retryable and sleeps == []


def test_transport_errors_are_retried_then_fail(make_client, sleeps) -> None:
    with socket.socket() as s:  # find a port nobody listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(TransportError, match="failed after 3 attempt"):
        make_client(max_retries=2, request_timeout_s=2).get(f"http://127.0.0.1:{port}/")
    assert len(sleeps) == 2


def test_response_too_large_by_content_length(httpserver: HTTPServer, make_client) -> None:
    httpserver.expect_request("/").respond_with_data("x" * 2000)
    with pytest.raises(ResponseTooLarge):
        make_client(max_response_bytes=1000).get(httpserver.url_for("/"))


def test_response_too_large_when_streamed(httpserver: HTTPServer, make_client) -> None:
    def chunked(_: Request) -> Response:
        return Response((b"x" * 500 for _ in range(10)), direct_passthrough=True)

    httpserver.expect_request("/").respond_with_handler(chunked)
    with pytest.raises(ResponseTooLarge):
        make_client(max_response_bytes=1000).get(httpserver.url_for("/"))


def test_byte_budget(httpserver: HTTPServer, make_client) -> None:
    httpserver.expect_request("/").respond_with_data("x" * 600)
    client = make_client(max_bytes=1000)
    client.get(httpserver.url_for("/"))
    with pytest.raises(BudgetExhausted) as ei:
        client.get(httpserver.url_for("/"))
    assert ei.value.kind == "bytes"


def test_time_budget(httpserver: HTTPServer, make_client) -> None:
    httpserver.expect_request("/").respond_with_json([])
    client = make_client()
    now = [0.0]
    client.budget = Budget(Limits(max_seconds=10), clock=lambda: now[0])
    client.get(httpserver.url_for("/"))
    now[0] = 11.0
    with pytest.raises(BudgetExhausted) as ei:
        client.get(httpserver.url_for("/"))
    assert ei.value.kind == "time"


def test_backoff_that_outlasts_the_budget_stops(
    httpserver: HTTPServer, make_client, sleeps
) -> None:
    httpserver.expect_request("/").respond_with_data("", status=429, headers={"Retry-After": "30"})
    client = make_client(max_seconds=5)
    with pytest.raises(BudgetExhausted):
        client.get(httpserver.url_for("/"))
    assert sleeps == []


def test_same_origin_redirect_keeps_auth(httpserver: HTTPServer, make_client) -> None:
    httpserver.expect_request("/a").respond_with_data("", status=302, headers={"Location": "/b"})
    httpserver.expect_request("/b").respond_with_json({"ok": True})
    resp = make_client().get(httpserver.url_for("/a"), headers={"Authorization": "Bearer t0ken"})
    assert resp.url.endswith("/b")
    assert httpserver.log[1][0].headers.get("Authorization") == "Bearer t0ken"


def test_cross_origin_redirect_strips_auth(httpserver: HTTPServer, make_client) -> None:
    # 127.0.0.1 and localhost are different origins on the same test server.
    other = f"http://localhost:{httpserver.port}/b"
    httpserver.expect_request("/a").respond_with_data("", status=302, headers={"Location": other})
    httpserver.expect_request("/b").respond_with_json({"ok": True})
    make_client().get(
        httpserver.url_for("/a"),
        headers={"Authorization": "Bearer t0ken", "X-Api-Key": "k3y-value"},
        sensitive_headers=["x-api-key"],
    )
    second = httpserver.log[1][0]
    assert "Authorization" not in second.headers
    assert "X-Api-Key" not in second.headers


def test_303_turns_post_into_get(httpserver: HTTPServer, make_client) -> None:
    httpserver.expect_request("/submit", method="POST").respond_with_data(
        "", status=303, headers={"Location": "/result"}
    )
    httpserver.expect_request("/result", method="GET").respond_with_json({"done": True})
    resp = make_client().post(httpserver.url_for("/submit"), json={"q": 1})
    assert resp.json() == {"done": True}


def test_too_many_redirects(httpserver: HTTPServer, make_client) -> None:
    httpserver.expect_request("/loop").respond_with_data(
        "", status=302, headers={"Location": "/loop"}
    )
    with pytest.raises(EgressBlocked) as ei:
        make_client(max_redirects=3).get(httpserver.url_for("/loop"))
    assert ei.value.code == "too_many_redirects"
    assert len(httpserver.log) == 4


def test_forbidden_request_headers_are_dropped(httpserver: HTTPServer, make_client) -> None:
    httpserver.expect_request("/").respond_with_json({})
    make_client().get(
        httpserver.url_for("/"), headers={"X-Forwarded-For": "1.2.3.4", "X-Custom": "yes"}
    )
    sent = httpserver.log[0][0].headers
    assert "X-Forwarded-For" not in sent
    assert sent["X-Custom"] == "yes"
    assert sent["User-Agent"].startswith("databolt-standard-patterns/")


def test_json_body_and_form_body(httpserver: HTTPServer, make_client) -> None:
    httpserver.expect_request("/j", method="POST", json={"a": [1, 2]}).respond_with_json({})
    httpserver.expect_request("/f", method="POST", data="x=1&y=two").respond_with_json({})
    c = make_client()
    c.post(httpserver.url_for("/j"), json={"a": [1, 2]})
    c.post(httpserver.url_for("/f"), data={"x": "1", "y": "two"})


def test_invalid_json_is_a_pattern_error(httpserver: HTTPServer, make_client) -> None:
    httpserver.expect_request("/").respond_with_data("<html>", content_type="text/html")
    with pytest.raises(Exception, match="not valid JSON") as ei:
        make_client().get(httpserver.url_for("/")).json()
    assert getattr(ei.value, "code", "") == "invalid_json"


# -- helpers -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("", None), ("7", 7.0), ("0", 0.0), ("soon", None)],
)
def test_parse_retry_after(value: str | None, expected: float | None) -> None:
    assert parse_retry_after(value) == expected


def test_parse_retry_after_past_date_is_zero() -> None:
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0


def test_full_jitter_bounds() -> None:
    for attempt in range(8):
        d = full_jitter(attempt, 0.5, 4.0)
        assert 0 <= d <= min(4.0, 0.5 * 2**attempt)


def test_retryable_statuses() -> None:
    assert all(is_retryable_status(s) for s in (429, 500, 502, 503, 504, 599))
    assert not any(is_retryable_status(s) for s in (400, 401, 403, 404, 501))


def test_rate_limiter_spaces_requests() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    rl = RateLimiter(4, clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        rl.wait()
    assert slept == [0.25, 0.25]


def test_link_header_parsing() -> None:
    header = (
        '<https://api.github.com/repos?page=2>; rel="next", '
        '<https://api.github.com/repos?page=5>; rel="last", </first>; rel="first prev"'
    )
    links = parse_link_header(header, "https://api.github.com/repos")
    assert links["next"] == "https://api.github.com/repos?page=2"
    assert links["last"].endswith("page=5")
    assert links["first"] == links["prev"] == "https://api.github.com/first"


def test_merge_params_replaces_existing_keys() -> None:
    import httpx

    url = merge_params(httpx.URL("https://x.test/p?a=1&b=2"), {"b": 3, "c": None, "d": [1, 2]})
    assert str(url) == "https://x.test/p?a=1&b=3&d=1&d=2"
