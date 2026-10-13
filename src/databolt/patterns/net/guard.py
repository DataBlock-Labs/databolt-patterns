"""The egress guard: keep pattern traffic off loopback, private and metadata addresses.

It mirrors DataBolt Studio's `lib/studio/ssrf.ts`, the same blocklist and host rules,
and closes the gap that file documents (a DNS rebind between validation and connect):

1. **Pre-flight.** Every URL (the first request and every redirect hop) is checked
   for scheme, blocked host suffixes, and every address its hostname resolves to.
2. **Connect-time pinning.** The HTTP transport's network backend resolves the host
   again, refuses if *any* address is blocked, and connects to a vetted IP literal.
   TLS still verifies against the hostname, so pinning does not weaken TLS.
3. **Post-connect peer check.** After the socket connects, the peer address the OS
   reports is checked once more.

`allow_private=True` turns the address checks off (tests, a trusted local network).
Studio passes its own list in `blocked_cidrs`, so both sides enforce one list.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable, Sequence
from typing import Any

import httpcore
import httpx
from pydantic import BaseModel, ConfigDict, Field

from databolt.patterns.errors import EgressBlocked

# Same ranges as ssrf.ts `ipBlocked`. IPv4-mapped (::ffff:a.b.c.d), IPv4-compatible
# (::a.b.c.d) and NAT64 (64:ff9b::/96) addresses are checked by their embedded IPv4.
DEFAULT_BLOCKED_CIDRS: tuple[str, ...] = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",  # CGNAT, including Tailscale 100.64/10
    "127.0.0.0/8",
    "169.254.0.0/16",  # link-local, including cloud metadata 169.254.169.254
    "172.16.0.0/12",
    "192.168.0.0/16",
    "224.0.0.0/3",  # multicast and reserved (224.0.0.0 - 255.255.255.255)
    "::/128",
    "::1/128",
    "fc00::/7",  # unique local
    "fe80::/10",  # link-local
    "fec0::/10",  # site-local (deprecated)
    "ff00::/8",  # multicast
)
BLOCKED_HOSTS = ("localhost",)
BLOCKED_HOST_SUFFIXES = (".localhost", ".internal", ".local")
ALLOWED_SCHEMES = ("http", "https")
# IPv6 prefixes whose last 32 bits embed an IPv4 address that is re-checked against the
# IPv4 entries: IPv4-mapped, IPv4-compatible, NAT64. Studio sends the same list.
DEFAULT_UNWRAP_IPV4: tuple[str, ...] = ("::ffff:0:0/96", "::/96", "64:ff9b::/96")

Resolver = Callable[[str, int], list[str]]


class EgressPolicy(BaseModel):
    """What the guard allows. Part of runner protocol v1 (`egress` in the request)."""

    model_config = ConfigDict(extra="allow")

    allow_private: bool = Field(
        False, description="Allow loopback/private/link-local/metadata destinations."
    )
    blocked_cidrs: list[str] | None = Field(
        None, description="Override the CIDR blocklist (Studio passes its own)."
    )
    unwrap_ipv4: list[str] | None = Field(
        None,
        description="IPv6 prefixes whose embedded IPv4 is re-checked (default: mapped, "
        "compatible, NAT64).",
    )
    max_redirects: int = Field(5, ge=0, le=20)
    trust_env: bool = Field(
        True, description="Honour HTTP(S)_PROXY / NO_PROXY from the environment."
    )


def system_resolver(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise httpx.ConnectError(f"could not resolve host {host!r}: {e.strerror}") from None
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _strip_host(host: str) -> str:
    return host.strip().lower().strip("[]").split("%")[0].rstrip(".")


class EgressGuard:
    def __init__(self, policy: EgressPolicy | None = None, resolver: Resolver | None = None):
        self.policy = policy or EgressPolicy()
        self.resolver: Resolver = resolver or system_resolver
        cidrs = (
            self.policy.blocked_cidrs
            if self.policy.blocked_cidrs is not None
            else DEFAULT_BLOCKED_CIDRS
        )
        self.networks = [ipaddress.ip_network(c, strict=False) for c in cidrs]
        unwrap = (
            self.policy.unwrap_ipv4 if self.policy.unwrap_ipv4 is not None else DEFAULT_UNWRAP_IPV4
        )
        self.unwrap = [
            n
            for n in (ipaddress.ip_network(c, strict=False) for c in unwrap)
            if isinstance(n, ipaddress.IPv6Network) and n.prefixlen <= 96
        ]

    # -- address rules -------------------------------------------------------------

    def ip_blocked(self, ip: str) -> bool:
        """True if an IP literal is in a blocked range. Unparseable counts as blocked."""
        try:
            addr = ipaddress.ip_address(_strip_host(ip))
        except ValueError:
            return True
        if any(addr.version == n.version and addr in n for n in self.networks):
            return True
        if isinstance(addr, ipaddress.IPv6Address) and any(addr in n for n in self.unwrap):
            return self.ip_blocked(str(ipaddress.IPv4Address(addr.packed[12:])))
        return False

    def check_host(self, host: str) -> None:
        h = _strip_host(host)
        if not h:
            raise EgressBlocked("Blocked host (empty)")
        if self.policy.allow_private:
            return
        if h in BLOCKED_HOSTS or h.endswith(BLOCKED_HOST_SUFFIXES):
            raise EgressBlocked(f"Blocked host {h!r}")

    def resolve_checked(self, host: str, port: int) -> list[str]:
        """Resolve `host` and return its addresses; refuse if any is blocked."""
        self.check_host(host)
        h = _strip_host(host)
        ips = [h] if _is_ip(h) else self.resolver(h, port)
        if not ips:
            raise EgressBlocked(f"Blocked host {h!r} (no addresses)")
        if not self.policy.allow_private:
            bad = [ip for ip in ips if self.ip_blocked(ip)]
            if bad:
                raise EgressBlocked(
                    f"Blocked address for {h!r} (private/loopback/link-local/metadata range)"
                )
        return ips

    def check_url(self, url: str | httpx.URL) -> httpx.URL:
        """Pre-flight check of a URL: scheme, host rules and DNS. Returns the parsed URL."""
        try:
            u = httpx.URL(str(url))
        except (httpx.InvalidURL, ValueError, TypeError):
            raise EgressBlocked("Invalid URL") from None
        if u.scheme not in ALLOWED_SCHEMES:
            raise EgressBlocked("Only http/https URLs are allowed")
        if not u.host:
            raise EgressBlocked("Invalid URL (no host)")
        port = u.port or (443 if u.scheme == "https" else 80)
        self.resolve_checked(u.host, port)
        return u

    def check_peer(self, ip: str) -> None:
        if not self.policy.allow_private and self.ip_blocked(ip):
            raise EgressBlocked("Blocked peer address after connect (DNS rebinding?)")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


class GuardedBackend(httpcore.NetworkBackend):
    """An httpcore network backend that pins each connection to a vetted IP.

    `trusted` lists (host, port) endpoints that bypass the address rules: the configured
    HTTP proxy, which the operator chose, and which resolves the destination itself.
    """

    def __init__(self, guard: EgressGuard, trusted: Iterable[tuple[str, int]] = ()) -> None:
        self.guard = guard
        self.trusted = {(_strip_host(h), p) for h, p in trusted}
        self._inner = httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        if (_strip_host(host), port) in self.trusted:
            return self._inner.connect_tcp(host, port, timeout, local_address, socket_options)
        ips = self.guard.resolve_checked(host, port)
        last: Exception | None = None
        for ip in ips:
            try:
                stream = self._inner.connect_tcp(ip, port, timeout, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                last = e
                continue
            peer = stream.get_extra_info("server_addr")
            if isinstance(peer, Sequence) and peer:
                try:
                    self.guard.check_peer(str(peer[0]))
                except EgressBlocked:
                    stream.close()
                    raise
            return stream
        assert last is not None
        raise last

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        raise EgressBlocked("Unix sockets are not allowed")

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)
