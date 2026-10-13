"""Auth for REST patterns: the config models (what Studio renders) and their runtime.

Secret fields hold `${secrets.<name>}` references, never values. Auth is attached
only to requests whose origin matches the configured `base_url`, so a paginator's
next-URL pointing elsewhere never receives credentials.
"""

from __future__ import annotations

import base64
import time
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from databolt.patterns.contract import RunContext, SecretRef, ui
from databolt.patterns.errors import HttpError, ValidationError


class _AuthBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoAuth(_AuthBase):
    """No authentication."""

    type: Literal["none"] = "none"


class BearerAuth(_AuthBase):
    """`Authorization: Bearer <token>`."""

    type: Literal["bearer"] = "bearer"
    token: SecretRef = "${secrets.token}"


class ApiKeyAuth(_AuthBase):
    """An API key sent in a header or a query parameter."""

    type: Literal["api_key"] = "api_key"
    name: str = Field("X-API-Key", description="Header or query parameter name.")
    value: SecretRef = "${secrets.api_key}"
    location: Literal["header", "query"] = "header"


class HttpBasicAuth(_AuthBase):
    """HTTP Basic: `Authorization: Basic base64(username:password)`."""

    type: Literal["http_basic"] = "http_basic"
    username: str = Field(..., description="Plain text, or a ${secrets.x} reference.")
    password: SecretRef = "${secrets.password}"


class OAuth2ClientCredentials(_AuthBase):
    """OAuth 2.0 client-credentials grant; the access token is fetched once per run."""

    type: Literal["oauth2_client_credentials"] = "oauth2_client_credentials"
    token_url: str = Field(..., json_schema_extra=ui(placeholder="https://auth.example.com/token"))
    client_id: str = Field(..., description="Plain text, or a ${secrets.x} reference.")
    client_secret: SecretRef = "${secrets.client_secret}"
    scope: str | None = None
    audience: str | None = None
    client_auth: Literal["basic", "body"] = Field(
        "basic", description="Send client credentials as HTTP Basic (RFC 6749) or in the body."
    )
    extra: dict[str, str] = Field(
        default_factory=dict, description="Extra form fields for the token request."
    )


AuthConfig = Annotated[
    NoAuth | BearerAuth | ApiKeyAuth | HttpBasicAuth | OAuth2ClientCredentials,
    Field(discriminator="type"),
]
AUTH_TYPES = ["none", "bearer", "api_key", "http_basic", "oauth2_client_credentials"]


def coerce_auth(v: Any) -> Any:
    """Accept shorthand: None or "none" -> NoAuth, "bearer" -> BearerAuth defaults."""
    if v is None:
        return {"type": "none"}
    if isinstance(v, str):
        return {"type": v}
    return v


# -- functional helpers (api.auth.bearer(...)) ---------------------------------------------


def none() -> NoAuth:
    return NoAuth()


def bearer(token: str = "${secrets.token}") -> BearerAuth:
    return BearerAuth(token=token)


def api_key(
    value: str = "${secrets.api_key}",
    *,
    name: str = "X-API-Key",
    location: Literal["header", "query"] = "header",
) -> ApiKeyAuth:
    return ApiKeyAuth(value=value, name=name, location=location)


def http_basic(username: str, password: str = "${secrets.password}") -> HttpBasicAuth:
    return HttpBasicAuth(username=username, password=password)


def oauth2_client_credentials(
    token_url: str,
    client_id: str,
    client_secret: str = "${secrets.client_secret}",
    *,
    scope: str | None = None,
    audience: str | None = None,
    client_auth: Literal["basic", "body"] = "basic",
) -> OAuth2ClientCredentials:
    return OAuth2ClientCredentials(
        token_url=token_url,
        client_id=client_id,
        client_secret=client_secret,
        scope=scope,
        audience=audience,
        client_auth=client_auth,
    )


# -- runtime -------------------------------------------------------------------------------


class Authenticator:
    """Applies one auth config to outgoing requests."""

    def __init__(self, cfg: BaseModel, ctx: RunContext) -> None:
        self.cfg = cfg
        self.ctx = ctx
        self.sensitive_headers: set[str] = set()
        self._token: str | None = None
        self._expires_at = 0.0

    def _secret(self, ref: str) -> str:
        value = self.ctx.render(ref)
        if not isinstance(value, str) or not value:
            raise ValidationError("an auth secret resolved to an empty value")
        self.ctx.redactor.add(value)
        return value

    def apply(self, headers: dict[str, str], params: dict[str, Any]) -> None:
        cfg = self.cfg
        if isinstance(cfg, BearerAuth):
            headers["authorization"] = f"Bearer {self._secret(cfg.token)}"
        elif isinstance(cfg, ApiKeyAuth):
            value = self._secret(cfg.value)
            if cfg.location == "header":
                headers[cfg.name.lower()] = value
                self.sensitive_headers.add(cfg.name.lower())
            else:
                params[cfg.name] = value
        elif isinstance(cfg, HttpBasicAuth):
            user = str(self.ctx.render(cfg.username))
            pwd = self._secret(cfg.password)
            self.ctx.redactor.add_basic(user, pwd)
            headers["authorization"] = (
                "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()
            )
        elif isinstance(cfg, OAuth2ClientCredentials):
            headers["authorization"] = f"Bearer {self._oauth_token(cfg)}"

    def _oauth_token(self, cfg: OAuth2ClientCredentials) -> str:
        if self._token and time.monotonic() < self._expires_at:
            return self._token
        client_id = str(self.ctx.render(cfg.client_id))
        secret = self._secret(cfg.client_secret)
        form: dict[str, str] = {"grant_type": "client_credentials"}
        if cfg.scope:
            form["scope"] = cfg.scope
        if cfg.audience:
            form["audience"] = cfg.audience
        form.update({k: str(self.ctx.render(v)) for k, v in cfg.extra.items()})
        headers: dict[str, str] = {"accept": "application/json"}
        if cfg.client_auth == "basic":
            self.ctx.redactor.add_basic(client_id, secret)
            pair = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
            headers["authorization"] = f"Basic {pair}"
        else:
            form.update({"client_id": client_id, "client_secret": secret})
        resp = self.ctx.http.post(str(self.ctx.render(cfg.token_url)), data=form, headers=headers)
        body = resp.json()
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise HttpError("token endpoint returned no access_token", http_status=resp.status_code)
        self.ctx.redactor.add(token)
        expires_in = body.get("expires_in") if isinstance(body, dict) else None
        ttl = float(expires_in) if isinstance(expires_in, (int, float)) else 3600.0
        self._token = token
        self._expires_at = time.monotonic() + max(0.0, ttl - 30)
        return token
