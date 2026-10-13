"""`databolt.ingestion.api.rest`: the standard REST pattern.

It manages auth, pagination (including auto-detection), retries and rate limits (via
the guarded client), and incremental cursors. Three ways in:

    api.rest(base_url=..., path=..., data_selector=..., max_pages=3).to_ndjson("out/")
    api.RestApiPattern(api.RestConfig.model_validate(cfg)).bind().iter_records()
    databolt-patterns run databolt.ingestion.api.rest -c config.yaml
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import httpx
from pydantic import BaseModel

from databolt.ingestion.api import auth as auth_mod
from databolt.ingestion.api import paginators as pag
from databolt.ingestion.api.config import RestConfig
from databolt.ingestion.api.incremental import Incremental, IncrementalConfig
from databolt.patterns.contract import Pattern, Record, RunContext, SecretSpec
from databolt.patterns.errors import HttpError
from databolt.patterns.limits import Limits
from databolt.patterns.net.client import HttpResponse
from databolt.patterns.net.guard import EgressPolicy
from databolt.patterns.net.ratelimit import RateLimiter
from databolt.patterns.redact import redact_url
from databolt.patterns.runtime import BoundPattern
from databolt.patterns.selectors import as_row, select_records

# Tried in order when no data_selector is set and the body is an object.
AUTO_DATA_KEYS = (
    "results",
    "data",
    "items",
    "records",
    "features",
    "entries",
    "value",
    "rows",
    "hits",
)


def join_url(base_url: str, path: str) -> str:
    if path.startswith(("http://", "https://")):
        return path
    if not path:
        return base_url
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def _origin(url: str) -> tuple[str, str, int | None]:
    u = httpx.URL(url)
    return (u.scheme, u.host, u.port or (443 if u.scheme == "https" else 80))


class RestApiPattern(Pattern):
    id = "databolt.ingestion.api.rest"
    version = "1.0.0"
    title = "REST API"
    description = (
        "Pull records from a REST/JSON API. Handles auth, pagination (auto-detected or "
        "explicit), retries with backoff on 429/5xx, rate limits and incremental cursors."
    )
    category = "ingestion"
    icon = "globe"
    Params = RestConfig
    secrets = (
        SecretSpec(
            name="token",
            description="Referenced by ${secrets.<name>} in params; any name works.",
            required=False,
        ),
    )
    capabilities = {
        "auth": auth_mod.AUTH_TYPES,
        "pagination": pag.PAGINATOR_TYPES,
        "incremental": True,
        "preview": True,
        "retries": True,
        "rate_limit": True,
        "methods": ["GET", "POST"],
    }
    examples = (
        {
            "title": "PokeAPI (auto pagination)",
            "params": {
                "client": {"base_url": "https://pokeapi.co/api/v2/", "paginator": "auto"},
                "resource": {"path": "pokemon", "params": {"limit": 100}},
                "max_pages": 3,
            },
        },
        {
            "title": "USGS earthquakes (incremental)",
            "params": {
                "client": {"base_url": "https://earthquake.usgs.gov/fdsnws/event/1/"},
                "resource": {
                    "path": "query",
                    "params": {"format": "geojson", "orderby": "time-asc"},
                    "data_selector": "features",
                    "paginator": {"type": "offset", "offset": 1, "limit": 1000},
                    "incremental": {
                        "param": "starttime",
                        "cursor_path": "properties.time",
                        "initial_value": "2026-10-01T00:00:00Z",
                        "transform": "epoch_ms_to_iso",
                        "order": "asc",
                    },
                },
            },
        },
    )

    def __init__(self, config: RestConfig | Mapping[str, Any] | None = None) -> None:
        self.config = None if config is None else self.parse_params(config)

    def bind(self, **kwargs: Any) -> BoundPattern:
        """A BoundPattern for the config given to the constructor."""
        return BoundPattern(self, self.config, **kwargs)

    def source_label(self, params: BaseModel) -> str:
        assert isinstance(params, RestConfig)
        if "${" in params.client.base_url or "${" in params.resource.path:
            return ""
        return redact_url(join_url(params.client.base_url, params.resource.path))

    def iter_records(self, params: BaseModel, ctx: RunContext) -> Iterator[Record]:
        assert isinstance(params, RestConfig)
        cfg = params
        res = cfg.resource
        ctx.cap_pages(cfg.max_pages)
        ctx.cap_records(cfg.max_records)
        if cfg.requests_per_second:
            ctx.http.rate_limiter = RateLimiter(cfg.requests_per_second)

        inc: Incremental | None = None
        extra_state: dict[str, Any] = {}
        if res.incremental is not None:
            inc = Incremental(res.incremental, ctx.state)
            ctx.track_state(inc)
            extra_state["cursor"] = inc.param_value()

        base_url = str(ctx.render(cfg.client.base_url))
        url = join_url(base_url, str(ctx.render(res.path)))
        ctx.stats.source_label = redact_url(url)
        headers = {k: str(v) for k, v in ctx.render(cfg.client.headers).items()}
        query = dict(ctx.render(res.params, extra_state=extra_state))
        body = ctx.render(res.json_body, extra_state=extra_state)
        if inc is not None and res.incremental is not None and res.incremental.param:
            start = inc.param_value()
            if start is not None:
                query[res.incremental.param] = start

        authn = auth_mod.Authenticator(cfg.client.auth, ctx)
        base_origin = _origin(base_url)
        paginator = pag.build(cfg.effective_paginator)
        ignore = res.response_actions
        ignore_statuses = {a.status_code for a in ignore if a.status_code is not None}

        req = pag.PageRequest(url, query, body)
        paginator.start(req)
        seen: set[str] = set()
        emitted = 0
        pages = 0
        while True:
            seen.add(req.signature())
            req_headers = dict(headers)
            req_params = dict(req.params)
            if _origin(req.url) == base_origin:
                authn.apply(req_headers, req_params)
            elif not isinstance(cfg.client.auth, auth_mod.NoAuth):
                ctx.warn(
                    f"not sending credentials to {redact_url(req.url)}: a different origin "
                    "from base_url"
                )
            resp = ctx.http.request(
                res.method,
                req.url,
                params=req_params,
                headers=req_headers,
                json=req.json,
                sensitive_headers=authn.sensitive_headers,
                allow_statuses=ignore_statuses,
            )
            pages += 1
            ctx.stats.pages = pages
            if self._ignored(resp, ignore):
                ctx.log(f"page {pages}: HTTP {resp.status_code} ignored by a response action")
                break
            if not resp.ok:  # allowed through for a response action that did not match
                raise HttpError(
                    f"{res.method} {redact_url(req.url)} returned HTTP {resp.status_code}",
                    http_status=resp.status_code,
                )
            data = resp.json()
            records = self._select(data, res.data_selector, ctx)
            if isinstance(paginator, pag.Auto) and pages == 1:
                nxt = paginator.next(req, resp, data, len(records))
                ctx.stats.paginator_detected = paginator.detected
                if paginator.note:
                    ctx.warn(paginator.note)
            else:
                nxt = paginator.next(req, resp, data, len(records))
                if ctx.stats.paginator_detected is None:
                    ctx.stats.paginator_detected = paginator.name
            ctx.log(f"page {pages}: {len(records)} records")

            for i, rec in enumerate(records):
                if inc is not None and not inc.keep(rec):
                    continue
                if ctx.max_records is not None and emitted >= ctx.max_records:
                    ctx.truncated = True
                    return
                yield rec
                emitted += 1
                if ctx.max_records is not None and emitted >= ctx.max_records:
                    remaining = records[i + 1 :]
                    if nxt is not None or any(inc is None or inc.keep(r) for r in remaining):
                        ctx.truncated = True
                    return

            if nxt is None:
                break
            if nxt.signature() in seen:
                ctx.warn("pagination returned a page that was already fetched; stopping")
                break
            if ctx.max_pages is not None and pages >= ctx.max_pages:
                ctx.truncated = True
                break
            req = nxt

    @staticmethod
    def _ignored(resp: HttpResponse, actions: list[Any]) -> bool:
        for a in actions:
            if a.status_code is not None and a.status_code != resp.status_code:
                continue
            if a.content is not None and a.content not in resp.text:
                continue
            return True
        return False

    @staticmethod
    def _select(data: Any, selector: str | None, ctx: RunContext) -> list[Record]:
        if selector:
            return select_records(data, selector)
        if isinstance(data, list):
            return [as_row(x) for x in data]
        if isinstance(data, dict):
            for key in AUTO_DATA_KEYS:
                if isinstance(data.get(key), list):
                    ctx.stats.warn(f"data_selector not set; using '{key}'")
                    return [as_row(x) for x in data[key]]
            return [data]
        if data is None:
            return []
        return [{"value": data}]


def rest(
    base_url: str,
    path: str = "",
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    json: Any = None,
    data_selector: str | None = None,
    paginator: str | BaseModel | Mapping[str, Any] = "auto",
    auth: BaseModel | Mapping[str, Any] | None = None,
    incremental: IncrementalConfig | Mapping[str, Any] | None = None,
    response_actions: list[Mapping[str, Any]] | None = None,
    max_pages: int | None = None,
    max_records: int | None = None,
    requests_per_second: float | None = None,
    secrets: Mapping[str, str] | None = None,
    state: dict[str, Any] | None = None,
    limits: Limits | None = None,
    egress: EgressPolicy | None = None,
) -> BoundPattern:
    """The functional form of the REST pattern.

    >>> from databolt.ingestion import api
    >>> src = api.rest(base_url="https://pokeapi.co/api/v2/", path="pokemon",
    ...                data_selector="results", max_pages=3)
    >>> rows = list(src.iter_records())
    """

    def dump(v: Any) -> Any:
        return v.model_dump(by_alias=True) if isinstance(v, BaseModel) else v

    config: dict[str, Any] = {
        "client": {
            "base_url": base_url,
            "headers": dict(headers or {}),
            "auth": dump(auth),
            "paginator": dump(paginator),
        },
        "resource": {
            "path": path,
            "method": method.upper(),
            "params": dict(params or {}),
            "json": json,
            "data_selector": data_selector,
            "incremental": dump(incremental),
            "response_actions": list(response_actions or []),
        },
        "max_pages": max_pages,
        "max_records": max_records,
        "requests_per_second": requests_per_second,
    }
    return BoundPattern(
        RestApiPattern(), config, secrets=secrets, state=state, limits=limits, egress=egress
    )
