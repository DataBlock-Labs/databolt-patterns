from __future__ import annotations

from typing import Any

from databolt.ingestion.api import RestApiPattern
from databolt.patterns import EgressPolicy, Limits, RunResult, run
from databolt.patterns.sinks import MemorySink

LOCAL = EgressPolicy(allow_private=True, trust_env=False)


def run_rest(
    params: dict[str, Any],
    *,
    secrets: dict[str, str] | None = None,
    state: dict[str, Any] | None = None,
    limits: Limits | None = None,
) -> tuple[RunResult, list[dict[str, Any]]]:
    sink = MemorySink()
    res = run(
        RestApiPattern(),
        params,
        secrets=secrets,
        state=state,
        egress=LOCAL,
        sink=sink,
        limits=limits or Limits(backoff_base_s=0, backoff_max_s=0),
    )
    return res, sink.records
