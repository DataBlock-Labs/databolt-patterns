"""Where credentials may go: the one policy every outgoing request is checked against.

A *credential* is a secret value, or a value derived from one (a Basic-auth header, an
OAuth access token), tagged with the secret's name. Before each request, and again on
every redirect hop, the HTTP client looks for credential values in the headers, the query
string, the path and the body. If the policy does not allow a credential at that URL, the
client removes the header or query param that carries it, or refuses the request when the
credential sits in the path or body.

A credential is allowed at a URL only when all of these hold:

1. **No downgrade.** If the pattern's base URL is https, the URL is https too.
2. **Origin binding** (when a pattern sets one, as the REST pattern does): the URL has the
   base URL's origin (scheme, host and port), or its host matches `auth_hosts`.
3. **Host binding** (runner protocol `secret_hosts`): the host matches the list bound to
   that secret's name. A missing or empty list means the secret goes nowhere.

Host patterns are case-insensitive. An exact name matches only that host. A leading `*.`
matches any subdomain, at any depth, but not the apex.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping

import httpx

Origin = tuple[str, str, int]


def origin_of(url: str | httpx.URL) -> Origin:
    u = httpx.URL(str(url))
    return (u.scheme, u.host.lower(), u.port or (443 if u.scheme == "https" else 80))


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def host_matches(host: str, patterns: Iterable[str]) -> bool:
    """Exact host, or `*.suffix` for subdomains (never the apex, never an IP literal)."""
    h = host.lower().rstrip(".").strip("[]")
    ip = _is_ip(h)
    for raw in patterns:
        p = raw.strip().lower().rstrip(".")
        if not p:
            continue
        if p.startswith("*."):
            if ip:
                continue
            suffix = p[1:]  # ".example.com"
            if h.endswith(suffix) and len(h) > len(suffix):
                return True
        elif h == p:
            return True
    return False


class CredentialPolicy:
    def __init__(self, secret_hosts: Mapping[str, list[str]] | None = None) -> None:
        self.secret_hosts = (
            None if secret_hosts is None else {k: list(v or []) for k, v in secret_hosts.items()}
        )
        self.base: Origin | None = None
        self.auth_hosts: list[str] = []

    def bind_origin(self, base_url: str, auth_hosts: Iterable[str] = ()) -> None:
        """Restrict credentials to `base_url`'s origin (plus `auth_hosts`)."""
        self.base = origin_of(base_url)
        self.auth_hosts = list(auth_hosts)

    def secret_allows_host(self, name: str, host: str) -> bool:
        if self.secret_hosts is None:
            return True
        return host_matches(host, self.secret_hosts.get(name, []))

    def denial(self, name: str, url: str | httpx.URL) -> str | None:
        """Why `name` may not be sent to `url`, or None if it may."""
        u = httpx.URL(str(url))
        if self.base is not None:
            if self.base[0] == "https" and u.scheme != "https":
                return "https->http downgrade"
            if origin_of(u) != self.base and not host_matches(u.host, self.auth_hosts):
                return "not the base_url origin or an auth_hosts entry"
        if not self.secret_allows_host(name, u.host):
            return "host not in the secret's secret_hosts binding"
        return None
