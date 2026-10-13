"""`databolt.ingestion.api.http_get`: fetch one URL and parse it into records.

For a single file or endpoint (a CSV export, a JSON document, an NDJSON dump). Use the
REST pattern when the API paginates.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator, Mapping
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from databolt.ingestion.api import auth as auth_mod
from databolt.ingestion.api.auth import AuthConfig, NoAuth, coerce_auth
from databolt.patterns.contract import Pattern, Record, RunContext, ui
from databolt.patterns.errors import PatternError
from databolt.patterns.limits import Limits
from databolt.patterns.net.client import HttpResponse
from databolt.patterns.net.guard import EgressPolicy
from databolt.patterns.redact import redact_url
from databolt.patterns.runtime import BoundPattern
from databolt.patterns.selectors import as_row, select_records

Format = Literal["auto", "json", "ndjson", "csv", "tsv"]


class HttpGetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", title="HTTP GET")

    url: str = Field(..., json_schema_extra=ui(placeholder="https://example.com/data.csv"))
    format: Format = Field("auto", description="auto: from the content type, then the URL.")
    data_selector: str | None = Field(None, description="For JSON: dotted path to the records.")
    headers: dict[str, str] = Field(
        default_factory=dict, json_schema_extra=ui(widget="key-value", advanced=True)
    )
    params: dict[str, Any] = Field(default_factory=dict, json_schema_extra=ui(widget="key-value"))
    auth: AuthConfig = Field(default_factory=NoAuth)
    max_records: int | None = Field(None, ge=1)

    _coerce_auth = field_validator("auth", mode="before")(coerce_auth)


def detect_format(resp: HttpResponse, url: str) -> str:
    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype in ("application/x-ndjson", "application/ndjson", "application/jsonl"):
        return "ndjson"
    if ctype.endswith("json"):
        return "json"
    if ctype in ("text/csv", "application/csv"):
        return "csv"
    if ctype == "text/tab-separated-values":
        return "tsv"
    path = url.split("?")[0].lower()
    for ext, fmt in ((".ndjson", "ndjson"), (".jsonl", "ndjson"), (".json", "json"),
                     (".tsv", "tsv"), (".csv", "csv")):  # fmt: skip
        if path.endswith(ext):
            return fmt
    head = resp.content.lstrip()[:1]
    return "json" if head in (b"{", b"[") else "csv"


def parse(resp: HttpResponse, fmt: str, selector: str | None) -> list[Record]:
    if fmt == "json":
        return select_records(resp.json(), selector)
    text = resp.text
    if fmt == "ndjson":
        rows: list[Record] = []
        for n, line in enumerate(text.splitlines(), 1):
            if line.strip():
                try:
                    rows.append(as_row(json.loads(line)))
                except ValueError:
                    raise PatternError(f"line {n} is not valid JSON", code="invalid_json") from None
        return rows
    reader = csv.DictReader(io.StringIO(text), delimiter="\t" if fmt == "tsv" else ",")
    return [dict(r) for r in reader]


class HttpGetPattern(Pattern):
    id = "databolt.ingestion.api.http_get"
    version = "1.0.0"
    title = "HTTP GET (single file)"
    description = "Fetch one URL and parse it as JSON, NDJSON, CSV or TSV."
    category = "ingestion"
    icon = "download"
    Params = HttpGetConfig
    capabilities = {
        "auth": auth_mod.AUTH_TYPES,
        "formats": ["json", "ndjson", "csv", "tsv"],
        "pagination": False,
        "incremental": False,
        "preview": True,
        "retries": True,
    }
    examples = (
        {
            "title": "A public CSV",
            "params": {"url": "https://example.com/data.csv", "format": "csv"},
        },
    )

    def source_label(self, params: BaseModel) -> str:
        assert isinstance(params, HttpGetConfig)
        return "" if "${" in params.url else redact_url(params.url)

    def check_bindings(self, params: BaseModel, ctx: RunContext) -> None:
        from databolt.ingestion.api.rest import check_secret_hosts

        assert isinstance(params, HttpGetConfig)
        host = httpx.URL(str(ctx.render(params.url))).host
        check_secret_hosts(self.required_secrets(params), host, ctx, "url")

    def iter_records(self, params: BaseModel, ctx: RunContext) -> Iterator[Record]:
        assert isinstance(params, HttpGetConfig)
        ctx.cap_records(params.max_records)
        url = str(ctx.render(params.url))
        ctx.credential_policy.bind_origin(url)
        ctx.stats.source_label = redact_url(url)
        headers = {k: str(v) for k, v in ctx.render(params.headers).items()}
        query = dict(ctx.render(params.params))
        authn = auth_mod.Authenticator(params.auth, ctx)
        authn.apply(headers, query)
        resp = ctx.http.get(
            url, params=query, headers=headers, sensitive_headers=authn.sensitive_headers
        )
        ctx.stats.pages = 1
        ctx.stats.paginator_detected = "single_page"
        fmt = params.format if params.format != "auto" else detect_format(resp, url)
        yield from parse(resp, fmt, params.data_selector)


def http_get(
    url: str,
    *,
    format: Format = "auto",
    data_selector: str | None = None,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    auth: BaseModel | Mapping[str, Any] | None = None,
    secrets: Mapping[str, str] | None = None,
    limits: Limits | None = None,
    egress: EgressPolicy | None = None,
) -> BoundPattern:
    config = {
        "url": url,
        "format": format,
        "data_selector": data_selector,
        "headers": dict(headers or {}),
        "params": dict(params or {}),
        "auth": auth.model_dump() if isinstance(auth, BaseModel) else auth,
    }
    return BoundPattern(HttpGetPattern(), config, secrets=secrets, limits=limits, egress=egress)
