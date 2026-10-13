from __future__ import annotations

from typing import Any

import httpcore
import pytest
from pytest_httpserver import HTTPServer

from databolt.patterns.errors import EgressBlocked
from databolt.patterns.net.guard import (
    DEFAULT_BLOCKED_CIDRS,
    EgressGuard,
    EgressPolicy,
    GuardedBackend,
)
from tests.conftest import BLOCKLIST_WITHOUT_LOOPBACK

BLOCKED_IPS = [
    "127.0.0.1",
    "127.255.255.254",
    "169.254.169.254",
    "100.64.0.1",
    "100.127.255.255",
    "10.0.0.1",
    "172.16.0.1",
    "172.31.255.255",
    "192.168.1.1",
    "0.0.0.0",
    "224.0.0.1",
    "255.255.255.255",
    "::1",
    "[::1]",
    "::",
    "::ffff:127.0.0.1",
    "::ffff:169.254.169.254",
    "::127.0.0.1",
    "64:ff9b::7f00:1",
    "fd00::1",
    "fe80::1",
    "fe80::1%eth0",
    "fec0::1",
    "ff02::1",
    "not-an-ip",
]
ALLOWED_IPS = [
    "8.8.8.8",
    "93.184.216.34",
    "100.63.255.255",
    "100.128.0.1",
    "172.15.255.255",
    "172.32.0.1",
    "2606:4700:4700::1111",
    "::ffff:8.8.8.8",
]


@pytest.mark.parametrize("ip", BLOCKED_IPS)
def test_ip_blocked(ip: str) -> None:
    assert EgressGuard().ip_blocked(ip)


@pytest.mark.parametrize("ip", ALLOWED_IPS)
def test_ip_allowed(ip: str) -> None:
    assert not EgressGuard().ip_blocked(ip)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://localhost:8080/",
        "http://api.localhost/",
        "http://metadata.google.internal/",
        "http://printer.local/",
        "http://2130706433/",  # 127.0.0.1 as a decimal integer
        "http://0x7f.1/",  # inet_aton hex + short form
        "http://017700000001/",  # octal
        "http://10.1/",  # 10.0.0.1 short form
    ],
)
def test_check_url_blocks_private_destinations(url: str) -> None:
    with pytest.raises(EgressBlocked):
        EgressGuard().check_url(url)


@pytest.mark.parametrize("url", ["ftp://example.com/", "file:///etc/passwd", "gopher://x/", "::"])
def test_check_url_rejects_bad_schemes(url: str) -> None:
    with pytest.raises(EgressBlocked):
        EgressGuard().check_url(url)


def test_any_blocked_resolution_blocks_the_host() -> None:
    guard = EgressGuard(resolver=lambda host, port: ["93.184.216.34", "127.0.0.1"])
    with pytest.raises(EgressBlocked, match="Blocked address"):
        guard.check_url("https://mixed.example/")


def test_public_resolution_passes() -> None:
    guard = EgressGuard(resolver=lambda host, port: ["93.184.216.34"])
    assert guard.check_url("https://api.example.com/x").host == "api.example.com"


def test_allow_private_permits_loopback() -> None:
    guard = EgressGuard(EgressPolicy(allow_private=True))
    guard.check_url("http://127.0.0.1:9/")
    guard.check_url("http://localhost/")


def test_custom_blocklist_replaces_the_default() -> None:
    guard = EgressGuard(EgressPolicy(blocked_cidrs=["203.0.113.0/24"]))
    assert guard.ip_blocked("203.0.113.7")
    assert not guard.ip_blocked("10.0.0.1")
    # embedded-IPv4 forms are still checked against the custom list
    assert guard.ip_blocked("::ffff:203.0.113.7")


def test_default_blocklist_matches_studio() -> None:
    # The list Studio's lib/studio/ssrf.ts enforces; change both sides together.
    assert set(DEFAULT_BLOCKED_CIDRS) == {
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "224.0.0.0/3",
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "fec0::/10",
        "ff00::/8",
    }


def test_client_refuses_loopback_server_before_sending(httpserver: HTTPServer, make_client):
    httpserver.expect_request("/").respond_with_json({"ok": True})
    client = make_client(allow_private=False)
    with pytest.raises(EgressBlocked):
        client.get(httpserver.url_for("/"))
    assert len(httpserver.log) == 0


def test_redirect_to_metadata_is_blocked(httpserver: HTTPServer, make_client) -> None:
    httpserver.expect_request("/start").respond_with_data(
        "", status=302, headers={"Location": "http://169.254.169.254/latest/meta-data/"}
    )
    client = make_client(allow_private=False, blocked_cidrs=BLOCKLIST_WITHOUT_LOOPBACK)
    with pytest.raises(EgressBlocked):
        client.get(httpserver.url_for("/start"))
    assert len(httpserver.log) == 1


def test_redirect_to_localhost_name_is_blocked(httpserver: HTTPServer, make_client) -> None:
    httpserver.expect_request("/start").respond_with_data(
        "", status=307, headers={"Location": f"http://localhost:{httpserver.port}/x"}
    )
    client = make_client(allow_private=False, blocked_cidrs=BLOCKLIST_WITHOUT_LOOPBACK)
    with pytest.raises(EgressBlocked, match="localhost"):
        client.get(httpserver.url_for("/start"))


def test_dns_rebinding_is_caught_at_connect(httpserver: HTTPServer, make_client) -> None:
    """Pre-flight sees a public IP; the connect-time resolution sees loopback."""
    httpserver.expect_request("/").respond_with_json({"secret": "internal"})
    answers = iter([["93.184.216.34"], ["127.0.0.1"]])
    client = make_client(allow_private=False, resolver=lambda host, port: next(answers))
    with pytest.raises(EgressBlocked):
        client.get(f"http://rebind.example:{httpserver.port}/")
    assert len(httpserver.log) == 0


class _FakeStream:
    def __init__(self, peer: str) -> None:
        self.peer = peer
        self.closed = False

    def get_extra_info(self, info: str) -> Any:
        return (self.peer, 443) if info == "server_addr" else None

    def close(self) -> None:
        self.closed = True


class _FakeInner:
    def __init__(self, peer: str) -> None:
        self.stream = _FakeStream(peer)
        self.connected_to: list[str] = []

    def connect_tcp(self, host: str, port: int, *a: Any, **k: Any) -> _FakeStream:
        self.connected_to.append(host)
        return self.stream


def test_post_connect_peer_check() -> None:
    guard = EgressGuard(resolver=lambda host, port: ["93.184.216.34"])
    backend = GuardedBackend(guard)
    inner = _FakeInner("127.0.0.1")
    backend._inner = inner  # type: ignore[assignment]
    with pytest.raises(EgressBlocked, match="peer"):
        backend.connect_tcp("api.example.com", 443)
    assert inner.stream.closed
    # and it pinned the connection to the vetted IP, not the hostname
    assert inner.connected_to == ["93.184.216.34"]


def test_backend_refuses_unix_sockets() -> None:
    with pytest.raises(EgressBlocked):
        GuardedBackend(EgressGuard()).connect_unix_socket("/var/run/docker.sock")


def test_backend_trusts_the_configured_proxy_endpoint() -> None:
    backend = GuardedBackend(EgressGuard(), trusted=[("10.0.0.5", 3128)])
    inner = _FakeInner("10.0.0.5")
    backend._inner = inner  # type: ignore[assignment]
    stream: Any = backend.connect_tcp("10.0.0.5", 3128)
    assert stream is inner.stream
    with pytest.raises(EgressBlocked):
        backend.connect_tcp("10.0.0.6", 3128)


def test_network_backend_is_an_httpcore_backend() -> None:
    assert isinstance(GuardedBackend(EgressGuard()), httpcore.NetworkBackend)


def test_unwrap_ipv4_is_configurable() -> None:
    no_unwrap = EgressGuard(EgressPolicy(unwrap_ipv4=[]))
    assert not no_unwrap.ip_blocked("::ffff:127.0.0.1")
    assert no_unwrap.ip_blocked("::1")  # still blocked as itself
    assert EgressGuard().ip_blocked("::ffff:7f00:1")


@pytest.mark.parametrize(
    ("host", "ip"),
    [("2130706433", "127.0.0.1"), ("0x7f.1", "127.0.0.1"), ("1.2.3", "1.2.0.3"),
     ("8.8.8.8", "8.8.8.8"), ("example.com", None), ("1.2.3.4.5", None), ("256.1.1.1", None)],
)  # fmt: skip
def test_legacy_ipv4_forms(host: str, ip: str | None) -> None:
    from databolt.patterns.net.guard import _legacy_ipv4

    assert _legacy_ipv4(host) == ip
