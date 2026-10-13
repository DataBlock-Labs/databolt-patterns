"""RestConfig: the params of the standard REST pattern, shaped like dlt's rest_api.

    client:    where and how to connect (base_url, headers, auth, paginator)
    resource:  what to fetch (path, params, body, data_selector, incremental, ...)

A flat form (`base_url`, `path`, ... at the top level) is accepted too and folded into
this shape, which is what the functional `api.rest(...)` builds.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from databolt.ingestion.api.auth import AuthConfig, NoAuth, coerce_auth
from databolt.ingestion.api.incremental import IncrementalConfig
from databolt.ingestion.api.paginators import AutoPaginator, PaginatorConfig, coerce_paginator
from databolt.patterns.contract import ui


class ResponseAction(BaseModel):
    """What to do with a matching response instead of failing. Today: `ignore`, which
    treats the page as empty and stops paginating (e.g. a 404 for "no data yet")."""

    model_config = ConfigDict(extra="forbid")

    status_code: int | None = Field(None, ge=100, le=599)
    content: str | None = Field(None, description="Match when the body contains this text.")
    action: Literal["ignore"] = "ignore"

    @model_validator(mode="after")
    def _needs_a_matcher(self) -> ResponseAction:
        if self.status_code is None and self.content is None:
            raise ValueError("a response action needs status_code and/or content")
        return self


class ClientConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(
        ...,
        description="API root, e.g. https://api.example.com/v1/",
        json_schema_extra=ui(placeholder="https://api.example.com/v1/", order=1),
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="Extra request headers. Values may use ${secrets.x}.",
        json_schema_extra=ui(widget="key-value", advanced=True),
    )
    auth: AuthConfig = Field(default_factory=NoAuth, json_schema_extra=ui(order=2))
    paginator: PaginatorConfig = Field(default_factory=AutoPaginator, json_schema_extra=ui(order=3))

    _coerce_auth = field_validator("auth", mode="before")(coerce_auth)
    _coerce_paginator = field_validator("paginator", mode="before")(coerce_paginator)


class ResourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    path: str = Field(
        "", description="Path under base_url (or an absolute URL).", json_schema_extra=ui(order=1)
    )
    method: Literal["GET", "POST"] = "GET"
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Query parameters. Values may use ${secrets.x} and ${state.cursor}.",
        json_schema_extra=ui(widget="key-value"),
    )
    json_body: Any = Field(
        None,
        alias="json",
        description="JSON request body (POST).",
        json_schema_extra=ui(widget="code", language="json", advanced=True),
    )
    data_selector: str | None = Field(
        None,
        description="Dotted path to the records, e.g. results or data.items[*]. Empty: the "
        "body itself if it is a list, else the first list under a common key.",
        json_schema_extra=ui(placeholder="results"),
    )
    paginator: PaginatorConfig | None = Field(
        None, description="Overrides client.paginator for this resource."
    )
    incremental: IncrementalConfig | None = None
    response_actions: list[ResponseAction] = Field(
        default_factory=list, json_schema_extra=ui(advanced=True)
    )

    _coerce_paginator = field_validator("paginator", mode="before")(coerce_paginator)


_CLIENT_KEYS = {"base_url", "headers", "auth"}
_RESOURCE_KEYS = {
    "path",
    "method",
    "params",
    "json",
    "json_body",
    "data_selector",
    "incremental",
    "response_actions",
}


class RestConfig(BaseModel):
    """Params of `databolt.ingestion.api.rest`."""

    model_config = ConfigDict(extra="forbid", title="REST API")

    client: ClientConfig
    resource: ResourceConfig = Field(default_factory=ResourceConfig)
    max_pages: int | None = Field(None, ge=1, description="Stop after this many pages.")
    max_records: int | None = Field(None, ge=1, description="Stop after this many records.")
    requests_per_second: float | None = Field(
        None, gt=0, description="Client-side rate limit.", json_schema_extra=ui(advanced=True)
    )

    @model_validator(mode="before")
    @classmethod
    def _fold_flat_form(cls, data: Any) -> Any:
        """Accept `{base_url, path, ...}` and dlt's `endpoint` as aliases of the nested shape."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if "endpoint" in data and "resource" not in data:
            data["resource"] = data.pop("endpoint")
        flat_client = {k: data.pop(k) for k in list(data) if k in _CLIENT_KEYS}
        flat_resource = {k: data.pop(k) for k in list(data) if k in _RESOURCE_KEYS}
        if "paginator" in data:
            flat_client["paginator"] = data.pop("paginator")
        if flat_client:
            data["client"] = {**flat_client, **dict(data.get("client") or {})}
        if flat_resource:
            data["resource"] = {**flat_resource, **dict(data.get("resource") or {})}
        return data

    @property
    def effective_paginator(self) -> BaseModel:
        return self.resource.paginator or self.client.paginator
