"""The guarded HTTP client patterns use (`ctx.http`).

It owns everything between "send this request" and "here are the bytes":

- egress checks on every hop (see guard.py), with redirects followed manually (at most
  `max_redirects`) and auth headers stripped when a hop changes origin;
- retries on 429/5xx and transport errors: full-jitter exponential backoff, honouring
  `Retry-After` in both its seconds and HTTP-date forms;
- an optional requests-per-second limit;
- a per-response byte cap (error) and the run's byte and time budget (truncation).
"""

from __future__ import annotations

import contextlib
import json as jsonlib
import os
import random
import ssl
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any

import certifi
import httpcore
import httpx

from databolt.patterns._version import __version__
from databolt.patterns.errors import (
    BudgetExhausted,
    CredentialBlocked,
    EgressBlocked,
    HttpError,
    PatternError,
    ResponseTooLarge,
    TransportError,
)
from databolt.patterns.limits import Budget
from databolt.patterns.net.backoff import full_jitter, is_retryable_status, parse_retry_after
from databolt.patterns.net.credentials import CredentialPolicy
from databolt.patterns.net.guard import EgressGuard, GuardedBackend
from databolt.patterns.net.ratelimit import RateLimiter
from databolt.patterns.redact import MIN_SECRET_LEN, Redactor, redact_url

USER_AGENT = f"databolt-standard-patterns/{__version__}"
# Never forwarded: a caller must not forge identity/forwarding headers (Studio's C13).
BLOCKED_REQUEST_HEADERS = frozenset(
    {"host", "cookie", "forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip"}
)
# Dropped when a redirect changes origin, plus whatever the auth marks sensitive.
CROSS_ORIGIN_STRIP = frozenset({"authorization", "proxy-authorization", "cookie"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

ParamValue = str | int | float | bool | None
Params = Mapping[str, ParamValue | list[ParamValue]]


class HttpResponse:
    """A fully read response. `url` is the final URL after redirects."""

    def __init__(self, status_code: int, headers: httpx.Headers, url: str, content: bytes):
        self.status_code = status_code
        self.headers = headers
        self.url = url
        self.content = content

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def text(self) -> str:
        charset = "utf-8"
        ctype = self.headers.get("content-type", "")
        for part in ctype.split(";")[1:]:
            k, _, v = part.strip().partition("=")
            if k.lower() == "charset" and v:
                charset = v.strip('"')
        return self.content.decode(charset, errors="replace")

    def json(self) -> Any:
        if not self.content.strip():
            return None
        try:
            return jsonlib.loads(self.text)
        except ValueError:
            raise PatternError(
                f"response from {redact_url(self.url)} is not valid JSON", code="invalid_json"
            ) from None

    @property
    def links(self) -> dict[str, str]:
        """RFC 8288 Link header as {rel: absolute url}."""
        return parse_link_header(self.headers.get("link", ""), self.url)


def parse_link_header(value: str, base: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in _split_links(value):
        if "<" not in part or ">" not in part:
            continue
        target = part[part.index("<") + 1 : part.index(">")]
        for param in part[part.index(">") + 1 :].split(";"):
            k, _, v = param.strip().partition("=")
            if k.strip().lower() == "rel":
                for rel in v.strip().strip('"').split():
                    out.setdefault(rel.lower(), str(httpx.URL(base).join(target)))
    return out


def _split_links(value: str) -> Iterator[str]:
    depth, start = 0, 0
    for i, ch in enumerate(value):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            yield value[start:i]
            start = i + 1
    if value[start:].strip():
        yield value[start:]


# -- transport: httpcore pool on the guarded network backend ----------------------------

_EXC_MAP: list[tuple[type[Exception], type[httpx.TransportError]]] = [
    (httpcore.ConnectTimeout, httpx.ConnectTimeout),
    (httpcore.ReadTimeout, httpx.ReadTimeout),
    (httpcore.WriteTimeout, httpx.WriteTimeout),
    (httpcore.PoolTimeout, httpx.PoolTimeout),
    (httpcore.TimeoutException, httpx.TimeoutException),
    (httpcore.ConnectError, httpx.ConnectError),
    (httpcore.ReadError, httpx.ReadError),
    (httpcore.WriteError, httpx.WriteError),
    (httpcore.NetworkError, httpx.NetworkError),
    (httpcore.ProxyError, httpx.ProxyError),
    (httpcore.UnsupportedProtocol, httpx.UnsupportedProtocol),
    (httpcore.RemoteProtocolError, httpx.RemoteProtocolError),
    (httpcore.LocalProtocolError, httpx.LocalProtocolError),
    (httpcore.ProtocolError, httpx.ProtocolError),
]


@contextlib.contextmanager
def _map_exceptions() -> Iterator[None]:
    try:
        yield
    except httpcore.TimeoutException as e:  # checked before the generic loop for clarity
        raise _mapped(e) from e
    except Exception as e:
        if isinstance(e, tuple(src for src, _ in _EXC_MAP)):
            raise _mapped(e) from e
        raise


def _mapped(e: Exception) -> httpx.TransportError:
    for src, dst in _EXC_MAP:
        if isinstance(e, src):
            return dst(str(e) or type(e).__name__)
    return httpx.TransportError(str(e))


class _ByteStream(httpx.SyncByteStream):
    def __init__(self, stream: Iterable[bytes]) -> None:
        self._stream = stream

    def __iter__(self) -> Iterator[bytes]:
        with _map_exceptions():
            yield from self._stream

    def close(self) -> None:
        close = getattr(self._stream, "close", None)
        if callable(close):
            close()


def _ssl_context(verify: bool | str) -> ssl.SSLContext:
    if verify is False:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if isinstance(verify, str):
        return ssl.create_default_context(cafile=verify)
    # Honour the standard OpenSSL overrides (corporate CAs), else certifi's bundle.
    cafile = os.environ.get("SSL_CERT_FILE")
    capath = os.environ.get("SSL_CERT_DIR")
    if cafile or capath:
        return ssl.create_default_context(cafile=cafile, capath=capath)
    return ssl.create_default_context(cafile=certifi.where())


class GuardedTransport(httpx.BaseTransport):
    """An httpx transport whose sockets come from the guard's pinned network backend."""

    def __init__(
        self, guard: EgressGuard, *, verify: bool | str = True, proxy: str | None = None
    ) -> None:
        ctx = _ssl_context(verify)
        trusted: list[tuple[str, int]] = []
        proxy_obj = None
        if proxy:
            pu = httpx.URL(proxy)
            trusted.append((pu.host, pu.port or (443 if pu.scheme == "https" else 80)))
            proxy_obj = httpcore.Proxy(url=proxy, ssl_context=ctx)
        self._pool = httpcore.ConnectionPool(
            ssl_context=ctx,
            proxy=proxy_obj,
            network_backend=GuardedBackend(guard, trusted),
            retries=0,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        assert isinstance(request.stream, httpx.SyncByteStream)
        req = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with _map_exceptions():
            resp = self._pool.handle_request(req)
        assert isinstance(resp.stream, Iterable)
        return httpx.Response(
            status_code=resp.status,
            headers=resp.headers,
            stream=_ByteStream(resp.stream),
            extensions=resp.extensions,
        )

    def close(self) -> None:
        self._pool.close()


def _env_proxy(url: httpx.URL) -> str | None:
    proxies = urllib.request.getproxies_environment()
    if not proxies:
        return None
    # proxy_bypass_environment exists on every platform but is missing from typeshed.
    bypass = getattr(urllib.request, "proxy_bypass_environment")  # noqa: B009
    if bypass(url.host, proxies):
        return None
    return proxies.get(url.scheme) or proxies.get("all")


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    default = 443 if url.scheme == "https" else 80
    return (url.scheme, url.host, url.port or default)


def _param_str(v: ParamValue) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def merge_params(url: httpx.URL, params: Params | None) -> httpx.URL:
    if not params:
        return url
    flat: list[tuple[str, str]] = []
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, list):
            flat.extend((k, _param_str(x)) for x in v if x is not None)
        else:
            flat.append((k, _param_str(v)))
    merged = httpx.QueryParams(url.params)
    for k in {k for k, _ in flat}:
        merged = merged.remove(k)
    for k, v in flat:
        merged = merged.add(k, v)
    return url.copy_with(query=str(merged).encode() or None)


class HttpClient:
    """Guarded, retrying, budgeted HTTP. One per run; patterns reach it as `ctx.http`."""

    def __init__(
        self,
        guard: EgressGuard | None = None,
        budget: Budget | None = None,
        redactor: Redactor | None = None,
        *,
        rate_limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        verify: bool | str = True,
        user_agent: str = USER_AGENT,
        policy: CredentialPolicy | None = None,
        credentials: Mapping[str, str] | None = None,
    ) -> None:
        self.guard = guard or EgressGuard()
        self.budget = budget or Budget()
        self.redactor = redactor or Redactor()
        self.rate_limiter = rate_limiter
        self.sleep = sleep
        self.rng = rng
        self.verify = verify
        self.user_agent = user_agent
        self._clients: dict[str | None, httpx.Client] = {}
        self.policy = policy or CredentialPolicy()
        # credential value -> secret name; see net/credentials.py
        self.credentials: dict[str, str] = {}
        for name, value in (credentials or {}).items():
            self.add_credential(name, value)

    def add_credential(self, name: str, value: str) -> None:
        """Track `value` (a secret, or something derived from one) as belonging to `name`."""
        if isinstance(value, str) and len(value) >= MIN_SECRET_LEN:
            self.credentials[value] = name

    @property
    def stats(self) -> Any:
        return self.budget.stats

    # -- public API ----------------------------------------------------------------

    def get(self, url: str, **kwargs: Any) -> HttpResponse:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> HttpResponse:
        return self.request("POST", url, **kwargs)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Params | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        data: Mapping[str, str] | None = None,
        sensitive_headers: Iterable[str] = (),
        allow_statuses: Iterable[int] = (),
    ) -> HttpResponse:
        """Send a request, following redirects and retrying; raise on a non-2xx status.

        Statuses in `allow_statuses` are returned instead of raised (e.g. a 404 that a
        response action ignores).
        """
        method = method.upper()
        target = merge_params(self._parse(url), params)
        hdrs = self._headers(headers)
        body: bytes | None = None
        if json is not None:
            body = jsonlib.dumps(json, separators=(",", ":")).encode()
            hdrs.setdefault("content-type", "application/json")
        elif data is not None:
            body = str(httpx.QueryParams(dict(data))).encode()
            hdrs.setdefault("content-type", "application/x-www-form-urlencoded")
        strip = CROSS_ORIGIN_STRIP | {h.lower() for h in sensitive_headers}
        allowed = frozenset(allow_statuses)
        limits = self.budget.limits

        attempt = 0
        while True:
            self.budget.check_time()
            if self.rate_limiter:
                self.rate_limiter.wait()
            try:
                resp = self._send(method, target, dict(hdrs), body, strip)
            except httpx.TransportError as e:
                if attempt >= limits.max_retries:
                    raise TransportError(
                        f"{method} {redact_url(str(target))} failed after {attempt + 1} "
                        f"attempt(s): {type(e).__name__}: {e}"
                    ) from None
                self._backoff(attempt, None)
                attempt += 1
                continue
            if resp.ok or resp.status_code in allowed:
                return resp
            retryable = is_retryable_status(resp.status_code)
            if retryable and attempt < limits.max_retries:
                self._backoff(attempt, resp.headers.get("retry-after"))
                attempt += 1
                continue
            snippet = resp.text[:200].strip().replace("\n", " ")
            raise HttpError(
                f"{method} {redact_url(str(target))} returned HTTP {resp.status_code}"
                + (f" after {attempt + 1} attempts" if attempt else "")
                + (f": {snippet}" if snippet else ""),
                http_status=resp.status_code,
                retryable=retryable,
            )

    def close(self) -> None:
        for c in self._clients.values():
            c.close()
        self._clients.clear()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals -----------------------------------------------------------------

    def _parse(self, url: str) -> httpx.URL:
        try:
            return httpx.URL(url)
        except (httpx.InvalidURL, ValueError, TypeError):
            raise EgressBlocked("Invalid URL") from None

    def _headers(self, headers: Mapping[str, str] | None) -> dict[str, str]:
        out = {"user-agent": self.user_agent, "accept": "application/json, */*;q=0.5"}
        for k, v in (headers or {}).items():
            if k.lower() not in BLOCKED_REQUEST_HEADERS:
                out[k.lower()] = v
        return out

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        limits = self.budget.limits
        wait = parse_retry_after(retry_after)
        if wait is None:
            wait = full_jitter(attempt, limits.backoff_base_s, limits.backoff_max_s, self.rng)
        elif wait > limits.max_retry_after_s:
            raise HttpError(
                f"server asked to retry after {wait:.0f}s, longer than the "
                f"{limits.max_retry_after_s:g}s limit",
                http_status=429,
                retryable=True,
            )
        if wait >= self.budget.remaining:
            raise BudgetExhausted("time", "time budget exhausted while backing off")
        self.stats.retries += 1
        self.sleep(wait)

    def _client_for(self, url: httpx.URL) -> httpx.Client:
        proxy = _env_proxy(url) if self.guard.policy.trust_env else None
        if proxy:
            self.stats.warn(
                "requests go through the configured HTTP proxy; the guard checks the "
                "destination's DNS but the post-connect peer check applies to the proxy"
            )
        client = self._clients.get(proxy)
        if client is None:
            client = httpx.Client(
                transport=GuardedTransport(self.guard, verify=self.verify, proxy=proxy),
                trust_env=False,
                follow_redirects=False,
            )
            self._clients[proxy] = client
        return client

    def _send(
        self,
        method: str,
        url: httpx.URL,
        headers: dict[str, str],
        body: bytes | None,
        strip: frozenset[str],
    ) -> HttpResponse:
        current = url
        for _hop in range(self.guard.policy.max_redirects + 1):
            self.guard.check_url(current)
            current = self._withhold_credentials(current, headers, body)
            client = self._client_for(current)
            req = client.build_request(
                method, current, headers=headers, content=body, timeout=self.budget.timeout()
            )
            self.stats.requests += 1
            resp = client.send(req, stream=True)
            try:
                location = resp.headers.get("location")
                if resp.status_code in REDIRECT_STATUSES and location:
                    nxt = current.join(location)
                    if _origin(nxt) != _origin(current):
                        for h in list(headers):
                            if h in strip:
                                del headers[h]
                    if resp.status_code == 303 or (
                        resp.status_code in (301, 302) and method == "POST"
                    ):
                        method, body = "GET", None
                        headers.pop("content-type", None)
                    current = nxt
                    continue
                content = self._read(resp, current)
                return HttpResponse(resp.status_code, resp.headers, str(current), content)
            finally:
                resp.close()
        raise EgressBlocked(
            f"too many redirects (> {self.guard.policy.max_redirects})", code="too_many_redirects"
        )

    def _withhold_credentials(
        self, url: httpx.URL, headers: dict[str, str], body: bytes | None
    ) -> httpx.URL:
        """Remove every credential the policy does not allow at `url` (mutates headers).

        A credential in a header or a query param is dropped with a warning. One in the
        path or the body cannot be removed safely, so the request is refused.
        """
        for value, name in self.credentials.items():
            reason = self.policy.denial(name, url)
            if reason is None:
                continue
            withheld = False
            for h in [h for h, v in headers.items() if value in v]:
                del headers[h]
                withheld = True
            params = url.params
            bad = {k for k, v in params.multi_items() if value in v}
            if bad:
                for k in bad:
                    params = params.remove(k)
                url = url.copy_with(query=str(params).encode() or None)
                withheld = True
            raw_path = url.raw_path.decode("ascii", "replace").split("?")[0]
            encoded = urllib.parse.quote(value, safe="")
            if value in raw_path or encoded in raw_path or (body and value.encode() in body):
                raise CredentialBlocked(f"refusing to send secret {name!r} to {url.host}: {reason}")
            if withheld:
                self.stats.warn(f"withheld secret {name!r} from {url.host}: {reason}")
        return url

    def _read(self, resp: httpx.Response, url: httpx.URL) -> bytes:
        cap = self.budget.limits.max_response_bytes
        clen = resp.headers.get("content-length", "")
        if clen.isdigit() and int(clen) > cap:
            raise ResponseTooLarge(
                f"response from {redact_url(str(url))} too large (> {cap} bytes)"
            )
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_bytes():
            total += len(chunk)
            if total > cap:
                raise ResponseTooLarge(
                    f"response from {redact_url(str(url))} too large (> {cap} bytes)"
                )
            self.budget.add_bytes(len(chunk))
            chunks.append(chunk)
        return b"".join(chunks)
