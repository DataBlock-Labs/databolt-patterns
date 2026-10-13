"""Paginators: config models plus the runtime that turns one page into the next request.

`auto` inspects the first response and picks, in order: an RFC 8288 `Link: rel="next"`
header, a next-page URL in the body, a cursor in the body, else a single page. The
choice is reported as `paginator_detected` in the run result.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

import httpx
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from databolt.patterns.net.client import HttpResponse
from databolt.patterns.selectors import get_path


class _PagBase(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class AutoPaginator(_PagBase):
    """Detect pagination from the first response (Link header, next URL, cursor)."""

    type: Literal["auto"] = "auto"


class SinglePagePaginator(_PagBase):
    """One request, no pagination."""

    type: Literal["single_page"] = "single_page"


class JsonLinkPaginator(_PagBase):
    """The body holds the next page's URL (e.g. `{"next": "https://.../?page=2"}`)."""

    type: Literal["json_link"] = "json_link"
    next_url_path: str = Field("next", description="Dotted path to the next URL in the body.")


class HeaderLinkPaginator(_PagBase):
    """RFC 8288 `Link` header (GitHub style)."""

    type: Literal["header_link"] = "header_link"
    rel: str = "next"


class HeaderCursorPaginator(_PagBase):
    """A response header carries the next cursor; it is sent back as a query param."""

    type: Literal["header_cursor"] = "header_cursor"
    cursor_header: str = "X-Next-Cursor"
    cursor_param: str = "cursor"


class CursorPaginator(_PagBase):
    """The body carries the next cursor; it is sent back as a query param or body field."""

    type: Literal["cursor"] = "cursor"
    cursor_path: str = Field("next_cursor", description="Dotted path to the cursor in the body.")
    cursor_param: str = Field("cursor", description="Query param (or body field) to send it as.")
    location: Literal["query", "json"] = "query"


class OffsetPaginator(_PagBase):
    """`?offset=N&limit=M`. Stops on a short or empty page, `total_path`, or `maximum_offset`."""

    type: Literal["offset"] = "offset"
    limit: int = Field(100, ge=1, description="Page size.")
    offset: int = Field(
        0,
        ge=0,
        description="First offset (the base): 0 for most APIs, 1 for e.g. USGS FDSN.",
        validation_alias=AliasChoices("offset", "base"),
    )
    offset_param: str = "offset"
    limit_param: str = "limit"
    total_path: str | None = Field(None, description="Dotted path to the total record count.")
    maximum_offset: int | None = Field(None, ge=0)


class PageNumberPaginator(_PagBase):
    """`?page=N`. Stops on an empty or short page, `total_path` (pages), or `maximum_page`."""

    type: Literal["page_number"] = "page_number"
    base_page: int = Field(1, ge=0)
    page_param: str = "page"
    page_size: int | None = Field(None, ge=1)
    page_size_param: str | None = None
    total_path: str | None = Field(None, description="Dotted path to the total page count.")
    maximum_page: int | None = Field(None, ge=0)


PaginatorConfig = Annotated[
    AutoPaginator
    | SinglePagePaginator
    | JsonLinkPaginator
    | HeaderLinkPaginator
    | HeaderCursorPaginator
    | CursorPaginator
    | OffsetPaginator
    | PageNumberPaginator,
    Field(discriminator="type"),
]
PAGINATOR_TYPES = [
    "auto",
    "single_page",
    "json_link",
    "header_link",
    "header_cursor",
    "cursor",
    "offset",
    "page_number",
]


def coerce_paginator(v: Any) -> Any:
    """Accept shorthand: "auto", "json_link", ... -> {"type": ...}."""
    if isinstance(v, str):
        return {"type": v}
    return v


# -- runtime -------------------------------------------------------------------------------


@dataclass
class PageRequest:
    url: str
    params: dict[str, Any] = field(default_factory=dict)
    json: Any = None

    def signature(self) -> str:
        return json.dumps([self.url, self.params, self.json], sort_keys=True, default=str)


class Paginator:
    name = "single_page"

    def start(self, req: PageRequest) -> None:
        """Set first-page params (offset/limit, page...)."""

    def next(self, req: PageRequest, resp: HttpResponse, data: Any, n: int) -> PageRequest | None:
        """The next request, or None when this was the last page. `n` = records on the page."""
        return None


def _absolute(base: str, target: str) -> str:
    return str(httpx.URL(base).join(target))


class JsonLink(Paginator):
    name = "json_link"

    def __init__(self, cfg: JsonLinkPaginator) -> None:
        self.path = cfg.next_url_path

    def next(self, req: PageRequest, resp: HttpResponse, data: Any, n: int) -> PageRequest | None:
        url = get_path(data, self.path)
        if not isinstance(url, str) or not url.strip():
            return None
        # The next URL carries its own query string; drop the first page's params.
        return PageRequest(_absolute(resp.url, url.strip()), {}, req.json)


class HeaderLink(Paginator):
    name = "header_link"

    def __init__(self, cfg: HeaderLinkPaginator) -> None:
        self.rel = cfg.rel.lower()

    def next(self, req: PageRequest, resp: HttpResponse, data: Any, n: int) -> PageRequest | None:
        url = resp.links.get(self.rel)
        return PageRequest(url, {}, req.json) if url else None


class HeaderCursor(Paginator):
    name = "header_cursor"

    def __init__(self, cfg: HeaderCursorPaginator) -> None:
        self.header = cfg.cursor_header
        self.param = cfg.cursor_param

    def next(self, req: PageRequest, resp: HttpResponse, data: Any, n: int) -> PageRequest | None:
        cursor = resp.headers.get(self.header)
        if not cursor:
            return None
        return PageRequest(req.url, {**req.params, self.param: cursor}, req.json)


class Cursor(Paginator):
    name = "cursor"

    def __init__(self, cfg: CursorPaginator) -> None:
        self.path = cfg.cursor_path
        self.param = cfg.cursor_param
        self.location = cfg.location

    def next(self, req: PageRequest, resp: HttpResponse, data: Any, n: int) -> PageRequest | None:
        cursor = get_path(data, self.path)
        if cursor is None or cursor == "" or isinstance(cursor, (bool, dict, list)):
            return None
        if self.location == "json":
            body = dict(req.json) if isinstance(req.json, dict) else {}
            body[self.param] = cursor
            return PageRequest(req.url, dict(req.params), body)
        return PageRequest(req.url, {**req.params, self.param: cursor}, req.json)


class Offset(Paginator):
    name = "offset"

    def __init__(self, cfg: OffsetPaginator) -> None:
        self.cfg = cfg
        self.offset = cfg.offset

    def start(self, req: PageRequest) -> None:
        req.params[self.cfg.offset_param] = self.offset
        req.params[self.cfg.limit_param] = self.cfg.limit

    def next(self, req: PageRequest, resp: HttpResponse, data: Any, n: int) -> PageRequest | None:
        if n == 0 or n < self.cfg.limit:
            return None
        self.offset += self.cfg.limit
        if self.cfg.total_path:
            total = get_path(data, self.cfg.total_path)
            if isinstance(total, (int, float)) and self.offset - self.cfg.offset >= total:
                return None
        if self.cfg.maximum_offset is not None and self.offset >= self.cfg.maximum_offset:
            return None
        return PageRequest(req.url, {**req.params, self.cfg.offset_param: self.offset}, req.json)


class PageNumber(Paginator):
    name = "page_number"

    def __init__(self, cfg: PageNumberPaginator) -> None:
        self.cfg = cfg
        self.page = cfg.base_page

    def start(self, req: PageRequest) -> None:
        req.params[self.cfg.page_param] = self.page
        if self.cfg.page_size and self.cfg.page_size_param:
            req.params[self.cfg.page_size_param] = self.cfg.page_size

    def next(self, req: PageRequest, resp: HttpResponse, data: Any, n: int) -> PageRequest | None:
        if n == 0 or (self.cfg.page_size and n < self.cfg.page_size):
            return None
        self.page += 1
        if self.cfg.total_path:
            total = get_path(data, self.cfg.total_path)
            pages_seen = self.page - self.cfg.base_page
            if isinstance(total, (int, float)) and pages_seen >= total:
                return None
        if self.cfg.maximum_page is not None and self.page > self.cfg.maximum_page:
            return None
        return PageRequest(req.url, {**req.params, self.cfg.page_param: self.page}, req.json)


# Body keys auto-detection looks at, most specific first.
NEXT_URL_PATHS = (
    "next",
    "next_url",
    "nextUrl",
    "next_page_url",
    "nextPageUrl",
    "@odata.nextLink",
    "nextLink",
    "links.next",
    "links.next.href",
    "_links.next.href",
    "_links.next",
    "paging.next",
    "pagination.next",
    "pagination.next_url",
    "meta.next",
)
# (cursor path, query param to send it as)
CURSOR_PATHS = (
    ("next_cursor", "cursor"),
    ("nextCursor", "cursor"),
    ("next_page_token", "page_token"),
    ("nextPageToken", "pageToken"),
    ("meta.next_cursor", "cursor"),
    ("meta.nextCursor", "cursor"),
    ("response_metadata.next_cursor", "cursor"),
    ("pagination.next_cursor", "cursor"),
    ("pagination.nextCursor", "cursor"),
    ("paging.cursors.after", "after"),
)


def _has_path(data: Any, path: str) -> bool:
    if isinstance(data, dict) and path in data:
        return True
    cur = data
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return False
        cur = cur[key]
    return True


def _url_like(v: Any) -> bool:
    return isinstance(v, str) and (v.startswith(("http://", "https://", "/", "?")))


class Auto(Paginator):
    """Delegates to the paginator detected from the first response."""

    name = "auto"

    def __init__(self) -> None:
        self.inner: Paginator | None = None
        self.detected = "single_page"
        self.note: str | None = None

    def detect(self, resp: HttpResponse, data: Any) -> Paginator:
        if "next" in resp.links:
            self.detected = "header_link"
            return HeaderLink(HeaderLinkPaginator())
        if isinstance(data, dict):
            for path in NEXT_URL_PATHS:
                if _has_path(data, path):
                    v = get_path(data, path)
                    if v is None or _url_like(v):
                        self.detected = "json_link"
                        return JsonLink(JsonLinkPaginator(next_url_path=path))
            for path, param in CURSOR_PATHS:
                if _has_path(data, path):
                    self.detected = "cursor"
                    self.note = (
                        f"auto-detected cursor pagination from '{path}', sent back as query "
                        f"param '{param}'; set the paginator explicitly if the API expects "
                        "another name"
                    )
                    return Cursor(CursorPaginator(cursor_path=path, cursor_param=param))
        self.detected = "single_page"
        return Paginator()

    def next(self, req: PageRequest, resp: HttpResponse, data: Any, n: int) -> PageRequest | None:
        if self.inner is None:
            self.inner = self.detect(resp, data)
        return self.inner.next(req, resp, data, n)


def build(cfg: BaseModel) -> Paginator:
    if isinstance(cfg, AutoPaginator):
        return Auto()
    if isinstance(cfg, JsonLinkPaginator):
        return JsonLink(cfg)
    if isinstance(cfg, HeaderLinkPaginator):
        return HeaderLink(cfg)
    if isinstance(cfg, HeaderCursorPaginator):
        return HeaderCursor(cfg)
    if isinstance(cfg, CursorPaginator):
        return Cursor(cfg)
    if isinstance(cfg, OffsetPaginator):
        return Offset(cfg)
    if isinstance(cfg, PageNumberPaginator):
        return PageNumber(cfg)
    return Paginator()
