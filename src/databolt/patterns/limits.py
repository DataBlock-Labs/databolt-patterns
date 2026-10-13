"""Run limits (part of runner protocol v1) and the per-run budget that enforces them."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from databolt.patterns.errors import BudgetExhausted

MiB = 1024 * 1024


class Limits(BaseModel):
    """Caps for one run. Page/record caps truncate; byte/time caps truncate too, with a warning.

    `max_response_bytes` is different: a single response over it is an error, because a
    partially read page cannot be parsed.
    """

    # Unknown keys are kept (and reported as warnings by the protocol), not rejected, so an
    # additive change on the caller's side cannot break an older package.
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    max_pages: int | None = Field(None, ge=1, description="Stop after this many pages.")
    max_records: int | None = Field(None, ge=1, description="Stop after this many records.")
    max_bytes: int = Field(
        256 * MiB,
        ge=1,
        description="Total response bytes per run.",
        validation_alias=AliasChoices("max_bytes", "max_total_bytes"),
    )
    max_seconds: float = Field(
        300.0,
        gt=0,
        description="Wall-clock budget per run.",
        validation_alias=AliasChoices("max_seconds", "total_timeout_s"),
    )
    max_response_bytes: int = Field(25 * MiB, ge=1, description="Cap on any single response.")
    request_timeout_s: float = Field(30.0, gt=0, description="Per-request timeout.")
    max_retries: int = Field(4, ge=0, le=10, description="Retries after the first attempt.")
    backoff_base_s: float = Field(0.5, ge=0)
    backoff_max_s: float = Field(30.0, ge=0)
    max_retry_after_s: float = Field(
        120.0, ge=0, description="Longest Retry-After the client will honour."
    )


def merge_cap(a: int | None, b: int | None) -> int | None:
    """The tighter of two optional caps."""
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


@dataclass
class RunStats:
    pages: int = 0
    requests: int = 0
    retries: int = 0
    bytes: int = 0
    paginator_detected: str | None = None
    source_label: str = ""
    warnings: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)


class Budget:
    """Tracks a run's elapsed time and bytes against its Limits."""

    def __init__(
        self,
        limits: Limits | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        stats: RunStats | None = None,
    ) -> None:
        self.limits = limits or Limits()
        self.clock = clock
        self.started = clock()
        self.stats = stats or RunStats()

    @property
    def elapsed(self) -> float:
        return self.clock() - self.started

    @property
    def remaining(self) -> float:
        return self.limits.max_seconds - self.elapsed

    def check_time(self) -> None:
        if self.remaining <= 0:
            raise BudgetExhausted("time", f"time budget of {self.limits.max_seconds:g}s exhausted")

    def add_bytes(self, n: int) -> None:
        self.stats.bytes += n
        if self.stats.bytes > self.limits.max_bytes:
            raise BudgetExhausted("bytes", f"byte budget of {self.limits.max_bytes} exhausted")

    def timeout(self) -> float:
        """Per-request timeout, never past the run deadline."""
        return max(0.001, min(self.limits.request_timeout_s, self.remaining))
