"""Manifest v1: the JSON description of every pattern that DataBolt Studio renders.

Each entry's `params_schema` is JSON Schema 2020-12 generated from the pattern's
Params model, plus `x-databolt-ui` hints. Secret params carry `format: secret-ref`;
auth and paginator choices are discriminated `oneOf`s. See docs/manifest.md.

Bump MANIFEST_VERSION only on a breaking change to this shape.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from databolt.patterns._version import __version__
from databolt.patterns.contract import Pattern
from databolt.patterns.protocol import PROTOCOL_VERSION
from databolt.patterns.registry import DIST_NAME, Registry

MANIFEST_VERSION = 1
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
OUTPUT_FORMATS = ["ndjson", "parquet", "duckdb"]


def params_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema(by_alias=True, mode="validation")
    return {"$schema": JSON_SCHEMA_DIALECT, **schema}


def _defaults(model: type[BaseModel]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, f in model.model_fields.items():
        if f.is_required():
            continue
        default = f.get_default(call_default_factory=True)
        if isinstance(default, BaseModel):
            default = default.model_dump(mode="json", by_alias=True, exclude_none=True)
        elif isinstance(default, list):
            default = [
                d.model_dump(mode="json", by_alias=True, exclude_none=True)
                if isinstance(d, BaseModel)
                else d
                for d in default
            ]
        if default is None:
            continue
        out[f.alias or name] = default
    return out


def pattern_entry(p: Pattern) -> dict[str, Any]:
    return {
        "id": p.id,
        "version": p.version,
        "title": p.title or p.id,
        "description": p.description,
        "category": p.category,
        "origin": p.origin,
        "provider": {"name": p.provider_name, "version": p.provider_version},
        "runtime": "python",
        "icon": p.icon,
        "params_schema": params_schema(p.Params),
        "secrets": [s.model_dump() for s in p.secrets],
        "defaults": _defaults(p.Params),
        "outputs": {"formats": OUTPUT_FORMATS, "record": "object"},
        "capabilities": dict(p.capabilities),
        "examples": [dict(e) for e in p.examples],
    }


def build_manifest(registry: Registry) -> dict[str, Any]:
    return {
        "manifest_version": MANIFEST_VERSION,
        "protocol": PROTOCOL_VERSION,
        "package": {"name": DIST_NAME, "version": __version__},
        "patterns": [pattern_entry(p) for p in registry.all()],
        "warnings": list(registry.warnings),
    }
