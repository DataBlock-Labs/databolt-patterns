"""databolt.patterns: the framework behind DataBolt ingestion patterns.

Write a pattern as a function:

    from pydantic import BaseModel
    from databolt.patterns import pattern, RunContext

    class Params(BaseModel):
        city: str

    @pattern(id="acme.weather", version="1.0.0")
    def weather(params: Params, ctx: RunContext):
        yield from ctx.http.get(f"https://api.example.com/{params.city}").json()["rows"]
"""

from databolt.patterns._version import __version__
from databolt.patterns.contract import (
    EmptyParams,
    Pattern,
    RunContext,
    SecretRef,
    SecretSpec,
    StateProposal,
    as_pattern,
    pattern,
    ui,
)
from databolt.patterns.errors import PatternError, ValidationError
from databolt.patterns.limits import Limits
from databolt.patterns.manifest import MANIFEST_VERSION, build_manifest
from databolt.patterns.net.guard import EgressPolicy
from databolt.patterns.protocol import PROTOCOL_VERSION
from databolt.patterns.registry import Registry, default_registry
from databolt.patterns.runtime import BoundPattern, RunResult, run

__all__ = [
    "MANIFEST_VERSION",
    "PROTOCOL_VERSION",
    "BoundPattern",
    "EgressPolicy",
    "EmptyParams",
    "Limits",
    "Pattern",
    "PatternError",
    "Registry",
    "RunContext",
    "RunResult",
    "SecretRef",
    "SecretSpec",
    "StateProposal",
    "ValidationError",
    "__version__",
    "as_pattern",
    "build_manifest",
    "default_registry",
    "pattern",
    "run",
    "ui",
]
