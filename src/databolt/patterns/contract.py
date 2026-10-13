"""What a pattern is: the Pattern base class, the @pattern decorator, and RunContext."""

from __future__ import annotations

import inspect
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Protocol, TextIO, TypeVar, get_type_hints

import pydantic
from pydantic import BaseModel, ConfigDict, Field

from databolt.patterns import templating
from databolt.patterns.errors import ValidationError
from databolt.patterns.limits import Budget, Limits, RunStats, merge_cap
from databolt.patterns.net.client import HttpClient
from databolt.patterns.net.credentials import CredentialPolicy
from databolt.patterns.net.guard import EgressGuard, EgressPolicy
from databolt.patterns.redact import Redactor

Record = dict[str, Any]

# A param that names a secret: its value is a `${secrets.<name>}` reference, never the
# secret itself. Studio renders these as a picker of keystore *names*.
SECRET_REF_SCHEMA: dict[str, Any] = {
    "format": "secret-ref",
    "x-databolt-ui": {"widget": "secret-ref"},
}
SecretRef = Annotated[
    str,
    Field(json_schema_extra=SECRET_REF_SCHEMA, pattern=r"^\$\{\s*secrets\.[A-Za-z0-9_\-]+\s*\}$"),
]


def ui(**hints: Any) -> dict[str, Any]:
    """`json_schema_extra` for a field's `x-databolt-ui` hints (widget, group, order...)."""
    return {"x-databolt-ui": hints}


class EmptyParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SecretSpec(BaseModel):
    """A secret a pattern can use. `name` is the key in the request's `secrets` object."""

    name: str
    description: str = ""
    required: bool = True


class StateTracker(Protocol):
    """Observes landed records and proposes the next incremental state."""

    def observe(self, record: Record) -> None: ...

    def proposal(self, truncated: bool) -> StateProposal: ...


@dataclass
class StateProposal:
    state: dict[str, Any] | None
    changed: bool
    warning: str | None = None


class RunContext:
    """Everything a pattern may touch during a run.

    Use `ctx.http` for all network access (it is egress-guarded, retrying and
    budgeted), `ctx.log` for progress (stderr, redacted), and `ctx.state` for the
    last committed state. Never write state yourself; propose it.
    """

    def __init__(
        self,
        *,
        params: BaseModel | None = None,
        secrets: Mapping[str, str] | None = None,
        state: dict[str, Any] | None = None,
        limits: Limits | None = None,
        egress: EgressPolicy | None = None,
        redactor: Redactor | None = None,
        budget: Budget | None = None,
        http: HttpClient | None = None,
        log_stream: TextIO | None = None,
        guard: EgressGuard | None = None,
        secret_hosts: Mapping[str, list[str]] | None = None,
    ) -> None:
        self.params = params
        self.secrets: Mapping[str, str] = dict(secrets or {})
        self.state = state
        self.limits = limits or Limits()
        self.egress = egress or EgressPolicy()
        self.redactor = redactor or Redactor(self.secrets.values())
        self.budget = budget or Budget(self.limits)
        self.guard = guard or EgressGuard(self.egress)
        self._http = http
        self._log_stream = log_stream
        # Where each secret may be sent (runner protocol `secret_hosts`); patterns may add
        # an origin binding (the REST pattern binds credentials to base_url's origin).
        self.credential_policy = CredentialPolicy(secret_hosts)
        self.max_pages: int | None = self.limits.max_pages
        self.max_records: int | None = self.limits.max_records
        self.truncated = False
        self.tracker: StateTracker | None = None
        self.proposed_state: dict[str, Any] | None = None
        self.state_proposed = False

    @property
    def stats(self) -> RunStats:
        return self.budget.stats

    @property
    def http(self) -> HttpClient:
        if self._http is None:
            self._http = HttpClient(
                self.guard,
                self.budget,
                self.redactor,
                policy=self.credential_policy,
                credentials=self.secrets,
            )
        return self._http

    def add_credential(self, secret_name: str, value: str) -> None:
        """Register a value derived from a secret (an auth header, an OAuth token) so it is
        redacted and only sent where that secret is allowed to go."""
        self.redactor.add(value)
        self.http.add_credential(secret_name, value)

    def close(self) -> None:
        if self._http is not None:
            self._http.close()

    def log(self, message: str) -> None:
        stream = self._log_stream or sys.stderr
        stream.write(f"[databolt] {self.redactor(message)}\n")
        stream.flush()

    def warn(self, message: str) -> None:
        self.stats.warn(self.redactor(message))

    def cap_pages(self, n: int | None) -> None:
        self.max_pages = merge_cap(self.max_pages, n)

    def cap_records(self, n: int | None) -> None:
        self.max_records = merge_cap(self.max_records, n)

    def render(self, obj: Any, *, extra_state: Mapping[str, Any] | None = None) -> Any:
        """Render `${params.*}`, `${secrets.*}` and `${state.*}` placeholders in `obj`."""
        params = self.params.model_dump(mode="json", by_alias=True) if self.params else {}
        state = dict(self.state or {})
        state.update(extra_state or {})
        return templating.render(obj, {"params": params, "secrets": self.secrets, "state": state})

    def track_state(self, tracker: StateTracker) -> None:
        """Hand the runtime an incremental tracker; it observes each *landed* record."""
        self.tracker = tracker

    def propose_state(self, state: dict[str, Any] | None) -> None:
        """Propose the state to commit if (and only if) this run lands successfully."""
        self.proposed_state = state
        self.state_proposed = True


class Pattern(ABC):
    """An ingestion pattern: validated params in, records out.

    Subclasses set the metadata attributes and implement `iter_records`. The manifest
    (what Studio renders) is derived from these attributes and the `Params` model.
    """

    id: str = ""
    version: str = "0.0.0"
    title: str = ""
    description: str = ""
    category: str = "ingestion"
    icon: str = "plug"
    Params: type[BaseModel] = EmptyParams
    secrets: tuple[SecretSpec, ...] = ()
    capabilities: Mapping[str, Any] = {}
    examples: tuple[Mapping[str, Any], ...] = ()
    # Set by the registry when the pattern is loaded.
    origin: str = "standard"
    provider_name: str = ""
    provider_version: str = ""

    def parse_params(self, raw: Mapping[str, Any] | BaseModel | None) -> BaseModel:
        if isinstance(raw, self.Params):
            return raw
        if isinstance(raw, BaseModel):
            raw = raw.model_dump(by_alias=True)
        try:
            return self.Params.model_validate(dict(raw or {}))
        except pydantic.ValidationError as e:
            raise ValidationError(format_validation_error(e, "params")) from None

    def required_secrets(self, params: BaseModel) -> set[str]:
        """Secret names this run needs. Defaults to the declared required secrets plus
        any `${secrets.x}` referenced in params."""
        declared = {s.name for s in self.secrets if s.required}
        return declared | templating.references(params.model_dump(by_alias=True), "secrets")

    def check_bindings(self, params: BaseModel, ctx: RunContext) -> None:  # noqa: B027
        """Fail fast, before any request, when a secret cannot go where it must
        (raise SecretHostMismatch). The default does nothing."""

    def source_label(self, params: BaseModel) -> str:
        """A redacted origin for `_source` (scheme://host/path, never a query string)."""
        return ""

    @abstractmethod
    def iter_records(self, params: BaseModel, ctx: RunContext) -> Iterator[Record]:
        """Yield records (dicts). Raise PatternError subclasses for expected failures."""

    def __repr__(self) -> str:
        return f"<Pattern {self.id}@{self.version} ({self.origin})>"


def format_validation_error(e: pydantic.ValidationError, prefix: str) -> str:
    """A pydantic error as `prefix.a.b: message; ...`, without echoing input values."""
    parts = []
    for err in e.errors():
        loc = ".".join(str(x) for x in (prefix, *err.get("loc", ())))
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "; ".join(parts[:10]) + (f"; (+{len(parts) - 10} more)" if len(parts) > 10 else "")


# -- the @pattern decorator ----------------------------------------------------------------

PatternFn = Callable[..., Iterable[Any]]
F = TypeVar("F", bound=PatternFn)


class FunctionPattern(Pattern):
    """A pattern backed by a plain function `fn(params, ctx) -> Iterable[dict]`."""

    fn: Callable[..., Iterable[Any]]

    def iter_records(self, params: BaseModel, ctx: RunContext) -> Iterator[Record]:
        for item in type(self).fn(params, ctx):
            yield item if isinstance(item, dict) else {"value": item}


def _params_model(fn: Callable[..., Any]) -> type[BaseModel]:
    sig = inspect.signature(fn)
    first = next(iter(sig.parameters.values()), None)
    if first is None:
        return EmptyParams
    try:
        hints = get_type_hints(fn)
    except Exception:  # unresolvable forward refs: fall back to the raw annotation
        hints = {}
    ann = hints.get(first.name, first.annotation)
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        return ann
    return EmptyParams


def pattern(
    *,
    id: str,
    version: str,
    title: str = "",
    description: str = "",
    category: str = "ingestion",
    icon: str = "code",
    secrets: Iterable[str | SecretSpec] = (),
    capabilities: Mapping[str, Any] | None = None,
    examples: Iterable[Mapping[str, Any]] = (),
) -> Callable[[F], F]:
    """Turn `fn(params: SomeModel, ctx: RunContext) -> Iterator[dict]` into a pattern.

    The params model comes from the first argument's type hint. The function is
    returned unchanged (still directly callable); the pattern class hangs off it as
    `fn.__databolt_pattern__`.
    """
    specs = tuple(s if isinstance(s, SecretSpec) else SecretSpec(name=s) for s in secrets)

    def deco(fn: F) -> F:
        doc = inspect.getdoc(fn) or ""
        cls = type(
            f"{fn.__name__}_pattern",
            (FunctionPattern,),
            {
                "id": id,
                "version": version,
                "title": title or fn.__name__.replace("_", " ").capitalize(),
                "description": description or doc,
                "category": category,
                "icon": icon,
                "Params": _params_model(fn),
                "secrets": specs,
                "capabilities": dict(capabilities or {}),
                "examples": tuple(examples),
                "fn": staticmethod(fn),
                "__module__": fn.__module__,
            },
        )
        fn.__databolt_pattern__ = cls  # type: ignore[attr-defined]
        return fn

    return deco


def as_pattern(obj: object) -> Pattern:
    """Coerce a Pattern instance, Pattern subclass or @pattern function to an instance."""
    if isinstance(obj, Pattern):
        return obj
    if isinstance(obj, type) and issubclass(obj, Pattern) and not inspect.isabstract(obj):
        return obj()
    cls = getattr(obj, "__databolt_pattern__", None)
    if isinstance(cls, type) and issubclass(cls, Pattern):
        return cls()
    raise ValidationError(f"{obj!r} is not a pattern (expected a Pattern subclass or @pattern fn)")
