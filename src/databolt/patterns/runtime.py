"""Run a pattern: validate, pull records, land them in a sink, propose state.

`run()` is what the CLI, the runner protocol and the Python API all call, so a
pattern behaves the same however it is invoked.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from pydantic import BaseModel

from databolt.patterns.contract import Pattern, Record, RunContext, as_pattern
from databolt.patterns.errors import BudgetExhausted, PatternError, ValidationError
from databolt.patterns.limits import Budget, Limits, merge_cap
from databolt.patterns.net.guard import EgressGuard, EgressPolicy
from databolt.patterns.protocol import PROTOCOL_VERSION
from databolt.patterns.redact import Redactor
from databolt.patterns.sinks import FileInfo, MemorySink, Sink, sink_for

PREVIEW_MAX_RECORDS = 50


@dataclass
class RunResult:
    ok: bool
    pattern: dict[str, str]
    files: list[FileInfo] = field(default_factory=list)
    rows: int = 0
    pages: int = 0
    requests: int = 0
    retries: int = 0
    bytes: int = 0
    paginator_detected: str | None = None
    source_label: str = ""
    state: dict[str, Any] | None = None
    state_changed: bool = False
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    records: list[Record] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": self.ok,
            "protocol": PROTOCOL_VERSION,
            "pattern": self.pattern,
            "files": [f.to_dict() for f in self.files],
            "rows": self.rows,
            "pages": self.pages,
            "requests": self.requests,
            "retries": self.retries,
            "bytes": self.bytes,
            "paginator_detected": self.paginator_detected,
            "source_label": self.source_label,
            "state": self.state,
            "state_changed": self.state_changed,
            "truncated": self.truncated,
            "warnings": self.warnings,
            "elapsed_ms": self.elapsed_ms,
        }
        if self.records is not None:
            out["records"] = self.records
        return out


def run(
    pattern: Pattern | Any,
    params: Mapping[str, Any] | BaseModel | None = None,
    *,
    secrets: Mapping[str, str] | None = None,
    state: dict[str, Any] | None = None,
    limits: Limits | None = None,
    egress: EgressPolicy | None = None,
    sink: Sink | None = None,
    preview: bool = False,
    log_stream: TextIO | None = None,
    redactor: Redactor | None = None,
    guard: EgressGuard | None = None,
    secret_hosts: Mapping[str, list[str]] | None = None,
) -> RunResult:
    """Run `pattern` once. Raises PatternError on failure (partial output is removed).

    `preview=True` caps the run at one page and 50 records, returns the records inline,
    and never proposes a state change.
    """
    started = time.monotonic()
    p = as_pattern(pattern)
    secrets = dict(secrets or {})
    limits = limits or Limits()
    if preview:
        limits = limits.model_copy(
            update={
                "max_pages": 1,
                "max_records": merge_cap(limits.max_records, PREVIEW_MAX_RECORDS),
            }
        )
    redactor = redactor or Redactor()
    for v in secrets.values():
        redactor.add(v)
    egress = egress or EgressPolicy()
    budget = Budget(limits)
    parsed = p.parse_params(params)
    missing = sorted(n for n in p.required_secrets(parsed) if n not in secrets)
    if missing:
        raise ValidationError(f"missing secret(s): {', '.join(missing)}")

    ctx = RunContext(
        params=parsed,
        secrets=secrets,
        state=state,
        limits=limits,
        egress=egress,
        redactor=redactor,
        budget=budget,
        log_stream=log_stream,
        guard=guard or EgressGuard(egress),
        secret_hosts=secret_hosts,
    )
    p.check_bindings(parsed, ctx)  # before any request: secret_host_mismatch is exit 2
    out = MemorySink() if preview or sink is None else sink
    budget.stats.source_label = p.source_label(parsed)
    truncated = False
    gen: Iterator[Record] = p.iter_records(parsed, ctx)
    try:
        out.open()
        count = 0
        try:
            for rec in gen:
                if ctx.max_records is not None and count >= ctx.max_records:
                    truncated = True
                    break
                out.write(rec)
                if ctx.tracker is not None:
                    ctx.tracker.observe(rec)
                count += 1
        except BudgetExhausted as e:
            truncated = True
            ctx.warn(f"stopped early: {e.message}")
        files = out.close()
    except BaseException:
        out.abort()
        raise
    finally:
        close = getattr(gen, "close", None)
        if callable(close):
            close()
        ctx.close()
    truncated = truncated or ctx.truncated

    new_state, changed = state, False
    if preview:
        pass
    elif ctx.tracker is not None:
        prop = ctx.tracker.proposal(truncated)
        # An unchanged proposal keeps the input state, whatever the tracker returned.
        new_state, changed = (prop.state, True) if prop.changed else (state, False)
        if prop.warning:
            ctx.warn(prop.warning)
    elif ctx.state_proposed:
        new_state = ctx.proposed_state
        changed = new_state != state

    stats = budget.stats
    return RunResult(
        ok=True,
        pattern={"id": p.id, "version": p.version},
        files=files,
        rows=out.rows,
        pages=stats.pages,
        requests=stats.requests,
        retries=stats.retries,
        bytes=stats.bytes,
        paginator_detected=stats.paginator_detected,
        source_label=stats.source_label,
        state=new_state,
        state_changed=changed,
        truncated=truncated,
        warnings=list(stats.warnings),
        elapsed_ms=int((time.monotonic() - started) * 1000),
        records=out.records if isinstance(out, MemorySink) and preview else None,
    )


class BoundPattern:
    """A pattern with its params bound: the functional API's return value.

    >>> src = api.rest(base_url="https://pokeapi.co/api/v2/", path="pokemon", max_pages=3)
    >>> for row in src.iter_records(): ...
    >>> src.to_parquet("pokemon.parquet")
    """

    def __init__(
        self,
        pattern: Pattern,
        params: Mapping[str, Any] | BaseModel | None = None,
        *,
        secrets: Mapping[str, str] | None = None,
        state: dict[str, Any] | None = None,
        limits: Limits | None = None,
        egress: EgressPolicy | None = None,
    ) -> None:
        self.pattern = pattern
        self.params = pattern.parse_params(params)
        self.secrets = dict(secrets or {})
        self.state = state
        self.limits = limits
        self.egress = egress
        self.last_result: RunResult | None = None

    def run(self, sink: Sink | None = None, *, preview: bool = False) -> RunResult:
        self.last_result = run(
            self.pattern,
            self.params,
            secrets=self.secrets,
            state=self.state,
            limits=self.limits,
            egress=self.egress,
            sink=sink,
            preview=preview,
        )
        return self.last_result

    def iter_records(self) -> Iterator[Record]:
        """All records, in memory. For large pulls, write to a file sink instead."""
        sink = MemorySink()
        self.run(sink)
        yield from sink.records

    def preview(self) -> RunResult:
        return self.run(preview=True)

    def to_ndjson(self, path: str | os.PathLike[str]) -> RunResult:
        return self.run(sink_for("ndjson", path))

    def to_parquet(self, path: str | os.PathLike[str]) -> RunResult:
        return self.run(sink_for("parquet", path))

    def to_duckdb(
        self, path: str | os.PathLike[str], table: str = "records", *, mode: str = "replace"
    ) -> RunResult:
        from databolt.patterns.sinks import DuckDBSink

        return self.run(DuckDBSink(Path(path), table=table, mode=mode))

    def commit(self) -> dict[str, Any] | None:
        """Adopt the last successful run's proposed state for the next run."""
        if self.last_result is not None and self.last_result.ok:
            self.state = self.last_result.state
        return self.state


def error_dict(e: BaseException, redactor: Redactor | None = None) -> dict[str, Any]:
    """The `error` object of a failed protocol result. Messages are always redacted."""
    r = redactor or Redactor()
    if isinstance(e, PatternError):
        return {
            "code": e.code,
            "message": r(e.message),
            "http_status": e.http_status,
            "retryable": e.retryable,
        }
    return {
        "code": "internal_error",
        "message": r(f"{type(e).__name__}: {e}"),
        "http_status": None,
        "retryable": False,
    }
