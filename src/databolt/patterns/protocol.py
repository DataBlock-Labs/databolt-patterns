"""Runner protocol v1: the stable stdin/stdout contract DataBolt Studio speaks.

- **stdin**: one JSON request.
- **stdout**: exactly one JSON result, then newline. Nothing else ever reaches stdout:
  `print()` in a pattern (and anything a child process writes to fd 1) goes to stderr.
- **stderr**: progress lines, redacted.
- **exit codes**: 0 ok, 2 validation, 3 runtime, 4 protocol mismatch.

Bump PROTOCOL_VERSION only on a breaking change to the request or result shape.
See docs/protocol.md for the full schema.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from typing import TYPE_CHECKING, Any, Literal, TextIO

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from databolt.patterns.contract import Pattern
    from databolt.patterns.redact import Redactor
    from databolt.patterns.registry import Registry

PROTOCOL_VERSION = 1

Op = Literal["run", "preview", "validate", "describe"]


class PatternRef(BaseModel):
    """Exactly one of: `id` (+ optional `version` range), `yaml` (a custom YAML pattern's
    text), or `file` + `entry` + `sha256` (a trusted custom Python file)."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    version: str | None = None
    yaml: str | None = None
    file: str | None = None
    entry: str | None = None
    sha256: str | None = None


class OutputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dir: str
    format: Literal["ndjson"] = "ndjson"


def _limits_type() -> Any:
    from databolt.patterns.limits import Limits

    return Limits


class Request(BaseModel):
    # Unknown top-level keys are ignored (with a warning) so an additive change on the
    # Studio side does not break an older package.
    model_config = ConfigDict(extra="allow")

    protocol: int
    op: Op
    run_id: str | None = None
    pattern: PatternRef | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)
    state: dict[str, Any] | None = None
    limits: dict[str, Any] = Field(default_factory=dict)
    egress: dict[str, Any] = Field(default_factory=dict)
    output: OutputSpec | None = None


def resolve_pattern(ref: PatternRef, registry: Registry) -> Pattern:
    from databolt.patterns.errors import ValidationError

    given = [k for k in ("id", "yaml", "file") if getattr(ref, k)]
    if len(given) != 1:
        raise ValidationError("pattern must have exactly one of id, yaml, file")
    if ref.id:
        return registry.get(ref.id, ref.version)
    if ref.yaml:
        return registry.load_yaml_text(ref.yaml)
    assert ref.file
    if not ref.sha256:
        raise ValidationError("a Python pattern file needs the sha256 the caller trusted")
    return registry.load_python_file(ref.file, entry=ref.entry, sha256=ref.sha256)


def handle(
    raw: str,
    *,
    registry: Registry | None = None,
    log_stream: TextIO | None = None,
    redactor: Redactor | None = None,
) -> tuple[dict[str, Any], int]:
    """Process one request. Returns (result, exit_code); never raises."""
    import pydantic

    from databolt.patterns.contract import format_validation_error
    from databolt.patterns.errors import (
        EXIT_OK,
        EXIT_RUNTIME,
        PatternError,
        ProtocolMismatch,
        ValidationError,
    )
    from databolt.patterns.redact import Redactor as _Redactor
    from databolt.patterns.runtime import error_dict

    started = time.monotonic()
    r = redactor or _Redactor()
    warnings: list[str] = []

    def fail(e: BaseException) -> tuple[dict[str, Any], int]:
        code = e.exit_code if isinstance(e, PatternError) else EXIT_RUNTIME
        return (
            {
                "ok": False,
                "protocol": PROTOCOL_VERSION,
                "error": error_dict(e, r),
                "warnings": [r(w) for w in warnings],
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            },
            code,
        )

    try:
        try:
            doc = json.loads(raw)
        except ValueError as e:
            raise ValidationError(
                f"request is not valid JSON: {e}", code="invalid_request"
            ) from None
        if not isinstance(doc, dict):
            raise ValidationError("request must be a JSON object", code="invalid_request")
        # Secrets first, so that even a malformed request cannot echo them.
        if isinstance(doc.get("secrets"), dict):
            for v in doc["secrets"].values():
                r.add(v)
        proto = doc.get("protocol")
        if proto != PROTOCOL_VERSION:
            raise ProtocolMismatch(
                f"request protocol {proto!r} is not supported; this package speaks "
                f"protocol {PROTOCOL_VERSION}"
            )
        try:
            req = Request.model_validate(doc)
        except pydantic.ValidationError as e:
            raise ValidationError(
                format_validation_error(e, "request"), code="invalid_request"
            ) from None
        if req.model_extra:
            warnings.append(
                f"ignored unknown request field(s): {', '.join(sorted(req.model_extra))}"
            )
        result = _dispatch(req, registry, log_stream, r, warnings)
        result.setdefault("warnings", [])
        result["warnings"] = [r(w) for w in [*warnings, *result["warnings"]]]
        result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return _redact_result(result, r), EXIT_OK
    except Exception as e:  # anything, expected or not, is still one JSON result
        return fail(e)


def _redact_result(result: dict[str, Any], r: Redactor) -> dict[str, Any]:
    """Mask secret values anywhere in the result (records can echo a token back)."""
    text = json.dumps(result, default=str)
    masked = r(text)
    if masked == text:
        return result
    try:
        out: dict[str, Any] = json.loads(masked)
    except ValueError:  # a secret spanning JSON syntax; fall back to field-level masking
        out = {k: (r(v) if isinstance(v, str) else v) for k, v in result.items()}
        out.pop("records", None)
    return out


def _dispatch(
    req: Request,
    registry: Registry | None,
    log_stream: TextIO | None,
    redactor: Redactor,
    warnings: list[str],
) -> dict[str, Any]:
    import pydantic

    from databolt.patterns.contract import format_validation_error
    from databolt.patterns.errors import ValidationError
    from databolt.patterns.limits import Limits
    from databolt.patterns.manifest import build_manifest, pattern_entry
    from databolt.patterns.net.guard import EgressPolicy
    from databolt.patterns.registry import default_registry
    from databolt.patterns.runtime import run
    from databolt.patterns.sinks import NdjsonSink

    reg = registry or default_registry()
    warnings.extend(reg.warnings)

    if req.op == "describe":
        if req.pattern is None:
            return {"ok": True, "protocol": PROTOCOL_VERSION, "manifest": build_manifest(reg)}
        p = resolve_pattern(req.pattern, reg)
        return {"ok": True, "protocol": PROTOCOL_VERSION, "manifest": pattern_entry(p)}

    if req.pattern is None:
        raise ValidationError(f"op {req.op!r} needs a pattern", code="invalid_request")
    p = resolve_pattern(req.pattern, reg)
    try:
        limits = Limits.model_validate(req.limits)
        egress = EgressPolicy.model_validate(req.egress)
        for label, model in (("limits", limits), ("egress", egress)):
            if model.model_extra:
                keys = ", ".join(sorted(model.model_extra))
                warnings.append(f"ignored unknown {label} field(s): {keys}")
    except pydantic.ValidationError as e:
        raise ValidationError(
            format_validation_error(e, "request"), code="invalid_request"
        ) from None

    if req.op == "validate":
        parsed = p.parse_params(req.params)
        needed = sorted(p.required_secrets(parsed))
        missing = [n for n in needed if n not in req.secrets]
        return {
            "ok": True,
            "protocol": PROTOCOL_VERSION,
            "pattern": {"id": p.id, "version": p.version},
            "required_secrets": needed,
            "warnings": [f"secret {n!r} is not provided" for n in missing],
        }

    sink = None
    if req.op == "run":
        if req.output is None:
            raise ValidationError("op 'run' needs output.dir", code="invalid_request")
        name = None
        if req.run_id:
            name = f"{_stem(p.id)}-{_stem(req.run_id)}.ndjson"
        sink = NdjsonSink(_out_dir(req.output.dir), stem=_stem(p.id), name=name)
    result = run(
        p,
        req.params,
        secrets=req.secrets,
        state=req.state,
        limits=limits,
        egress=egress,
        sink=sink,
        preview=req.op == "preview",
        log_stream=log_stream,
        redactor=redactor,
    )
    return result.to_dict()


def _out_dir(d: str) -> Any:
    from pathlib import Path

    return Path(d)


def _stem(pattern_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in pattern_id)


def main(stdin: TextIO | None = None) -> int:
    """Entry point for `databolt-patterns protocol`: stdin -> one JSON line on stdout."""
    from databolt.patterns.redact import RedactingStream, Redactor

    raw = (stdin or sys.stdin).read()
    redactor = Redactor()
    # Divert fd 1 to stderr for the duration of the run (covers print(), C extensions and
    # child processes), keeping a private handle on the real stdout for the result.
    sys.stdout.flush()
    real_fd = os.dup(1)
    os.dup2(2, 1)
    saved = sys.stdout
    sys.stdout = RedactingStream(sys.stderr, redactor)
    try:
        result, code = handle(raw, log_stream=sys.stderr, redactor=redactor)
    finally:
        sys.stdout.flush()
        sys.stdout = saved
        with contextlib.suppress(OSError):
            os.dup2(real_fd, 1)
    with os.fdopen(real_fd, "w", encoding="utf-8") as out:
        out.write(json.dumps(result, separators=(",", ":"), default=str))
        out.write("\n")
    return code
