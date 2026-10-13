"""Custom patterns: declarative YAML (`kind: databolt.pattern/v1`) and trusted Python files.

A YAML pattern **extends** an installed pattern. It declares its own friendly params,
and a `config` that becomes the base pattern's params once `${params.*}` is filled in.
`${secrets.*}` and `${state.*}` are left for run time. A YAML pattern runs no code of its
own, so it inherits the base pattern's egress guard and redaction.

A Python file is arbitrary code. It is executed only when its bytes hash to the sha256
the caller trusted, and it is the *verified bytes* that are executed, never a second read
of the file.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pydantic
import yaml
from pydantic import BaseModel, ConfigDict, Field, create_model

from databolt.patterns import semver, templating
from databolt.patterns.contract import (
    SECRET_REF_SCHEMA,
    FunctionPattern,
    Pattern,
    Record,
    RunContext,
    SecretRef,
    SecretSpec,
    as_pattern,
    format_validation_error,
)
from databolt.patterns.errors import TrustError, ValidationError

if TYPE_CHECKING:
    from databolt.patterns.registry import Registry

KIND = "databolt.pattern/v1"
MAX_YAML_BYTES = 256 * 1024
ID_PATTERN = r"^[a-z0-9][a-z0-9_\-]*(\.[a-z0-9][a-z0-9_\-]*)+$"

ParamType = Literal["string", "integer", "number", "boolean", "array", "object"]
_PY_TYPES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list[Any],
    "object": dict[str, Any],
}
_DUMMY: dict[str, Any] = {
    "string": "x",
    "integer": 1,
    "number": 1.0,
    "boolean": True,
    "array": [],
    "object": {},
}


class ParamSpec(BaseModel):
    """One exposed param of a YAML pattern (a small subset of JSON Schema)."""

    model_config = ConfigDict(extra="forbid")

    type: ParamType = "string"
    title: str | None = None
    description: str | None = None
    default: Any = None
    required: bool = False
    enum: list[Any] | None = None
    format: str | None = Field(None, description="e.g. secret-ref, date, date-time, uri")
    minimum: float | None = None
    maximum: float | None = None


class YamlPatternSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["databolt.pattern/v1"]
    id: str = Field(..., pattern=ID_PATTERN)
    version: str
    title: str = ""
    description: str = ""
    category: str = "ingestion"
    icon: str = "puzzle"
    extends: str
    extends_version: str | None = None
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    secrets: list[SecretSpec | str] = Field(default_factory=list)
    config: dict[str, Any]
    examples: list[dict[str, Any]] = Field(default_factory=list)


def _field(name: str, spec: ParamSpec) -> tuple[Any, Any]:
    if spec.format == "secret-ref":
        ann: Any = SecretRef
    elif spec.enum:
        ann = Literal[tuple(spec.enum)]
    else:
        ann = _PY_TYPES[spec.type]
    extra: dict[str, Any] = {}
    if spec.format == "secret-ref":
        extra.update(SECRET_REF_SCHEMA)  # re-stated: an outer Field replaces the Annotated one
    elif spec.format:
        extra["format"] = spec.format
    kwargs: dict[str, Any] = {
        "title": spec.title,
        "description": spec.description,
        "json_schema_extra": extra or None,
    }
    if spec.minimum is not None:
        kwargs["ge"] = spec.minimum
    if spec.maximum is not None:
        kwargs["le"] = spec.maximum
    if spec.required and spec.default is None:
        return (ann, Field(..., **kwargs))
    if spec.default is None:
        return (ann | None, Field(None, **kwargs))
    return (ann, Field(spec.default, **kwargs))


def build_params_model(pattern_id: str, params: Mapping[str, ParamSpec]) -> type[BaseModel]:
    fields = {name: _field(name, spec) for name, spec in params.items()}
    model_name = "".join(p.capitalize() for p in pattern_id.replace("-", "_").split(".")) + "Params"
    model: type[BaseModel] = create_model(  # type: ignore[call-overload]
        model_name, __config__=ConfigDict(extra="forbid"), **fields
    )
    return model


class YamlPattern(Pattern):
    """A pattern defined by a `databolt.pattern/v1` document."""

    def __init__(self, spec: YamlPatternSpec, base: Pattern, *, source: str = "") -> None:
        self.spec = spec
        self.base = base
        self.id = spec.id
        self.version = spec.version
        self.title = spec.title or spec.id
        self.description = spec.description
        self.category = spec.category
        self.icon = spec.icon
        self.Params = build_params_model(spec.id, spec.params)
        self.secrets = tuple(
            s if isinstance(s, SecretSpec) else SecretSpec(name=s) for s in spec.secrets
        )
        self.capabilities = {**dict(base.capabilities), "extends": base.id}
        self.examples = tuple(spec.examples)
        self.origin = "custom"
        self.provider_name = source or "custom"
        self.provider_version = spec.version

    def bind_config(self, params: BaseModel) -> BaseModel:
        """The base pattern's params: `config` with `${params.*}` filled in."""
        values = params.model_dump(mode="json", by_alias=True)
        rendered = templating.render(self.spec.config, {"params": values}, only={"params"})
        try:
            return self.base.parse_params(rendered)
        except ValidationError as e:
            raise ValidationError(
                f"{self.id}: config does not fit {self.base.id}: {e.message}"
            ) from None

    def required_secrets(self, params: BaseModel) -> set[str]:
        declared = {s.name for s in self.secrets if s.required}
        return declared | self.base.required_secrets(self.bind_config(params))

    def check_bindings(self, params: BaseModel, ctx: RunContext) -> None:
        self.base.check_bindings(self.bind_config(params), ctx)

    def source_label(self, params: BaseModel) -> str:
        try:
            return self.base.source_label(self.bind_config(params))
        except ValidationError:
            return ""

    def iter_records(self, params: BaseModel, ctx: RunContext) -> Iterator[Record]:
        yield from self.base.iter_records(self.bind_config(params), ctx)

    def check(self) -> None:
        """Validate `config` against the base pattern using placeholder param values."""
        dummy: dict[str, Any] = {}
        for name, spec in self.spec.params.items():
            if spec.default is not None:
                dummy[name] = spec.default
            elif spec.enum:
                dummy[name] = spec.enum[0]
            elif spec.format == "secret-ref":
                dummy[name] = "${secrets.placeholder}"
            else:
                dummy[name] = _DUMMY[spec.type]
        unknown = templating.references(self.spec.config, "params") - set(self.spec.params)
        if unknown:
            raise ValidationError(
                f"{self.id}: config references undeclared param(s): {', '.join(sorted(unknown))}"
            )
        try:
            params = self.Params.model_validate(dummy)
        except pydantic.ValidationError as e:
            raise ValidationError(format_validation_error(e, f"{self.id}.params")) from None
        self.bind_config(params)


def load_yaml_pattern(text: str, registry: Registry, *, source: str = "") -> YamlPattern:
    if len(text.encode()) > MAX_YAML_BYTES:
        raise ValidationError(f"custom pattern is larger than {MAX_YAML_BYTES // 1024} KiB")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValidationError(f"custom pattern is not valid YAML: {e}") from None
    if not isinstance(doc, dict):
        raise ValidationError("custom pattern must be a YAML mapping")
    if doc.get("kind") != KIND:
        raise ValidationError(f"custom pattern must have kind: {KIND}")
    try:
        spec = YamlPatternSpec.model_validate(doc)
    except pydantic.ValidationError as e:
        raise ValidationError(format_validation_error(e, "pattern")) from None
    semver.parse(spec.version)
    if spec.extends == spec.id:
        raise ValidationError(f"{spec.id} cannot extend itself")
    base = registry.get(spec.extends, spec.extends_version)
    p = YamlPattern(spec, base, source=source)
    p.check()
    return p


def sha256_of(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_python_patterns(
    path: str | Path, *, sha256: str, entry: str | None = None
) -> list[Pattern]:
    """Execute a trusted Python file and return its pattern(s).

    The bytes are hashed and compared with `sha256` *before* anything runs; the same
    in-memory bytes are then executed, so editing the file after the check changes nothing.
    """
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as e:
        raise ValidationError(f"cannot read {p}: {e.strerror}") from None
    digest = hashlib.sha256(data).hexdigest()
    if digest != sha256.strip().lower():
        raise TrustError(
            f"{p.name} has sha256 {digest[:12]}…, not the trusted {sha256[:12]}…; "
            "review and trust the new version before running it"
        )
    name = f"databolt_custom_{p.stem.replace('-', '_')}_{digest[:12]}"
    spec = importlib.util.spec_from_loader(name, loader=None, origin=str(p))
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(p)
    sys.modules[name] = module
    try:
        code = compile(data, str(p), "exec")
        exec(code, module.__dict__)  # noqa: S102 - the bytes were sha256-verified above
    except Exception as e:
        sys.modules.pop(name, None)
        raise ValidationError(f"{p.name} failed to load: {type(e).__name__}: {e}") from None

    if entry:
        if not hasattr(module, entry):
            raise ValidationError(f"{p.name} has no {entry!r}")
        found = [as_pattern(getattr(module, entry))]
    else:
        found = []
        for obj in vars(module).values():
            cls = getattr(obj, "__databolt_pattern__", None)
            if isinstance(cls, type) and issubclass(cls, FunctionPattern):
                found.append(cls())
            elif (
                isinstance(obj, type)
                and issubclass(obj, Pattern)
                and obj.__module__ == name
                and obj.id
            ):
                found.append(obj())
        if not found:
            raise ValidationError(f"{p.name} defines no @pattern function or Pattern class")
    for pat in found:
        pat.origin = "custom"
        pat.provider_name = p.name
        pat.provider_version = digest[:12]
    return found
