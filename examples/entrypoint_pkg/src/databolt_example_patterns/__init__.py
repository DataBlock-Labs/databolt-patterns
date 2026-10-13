"""Example patterns shipped in a pip package. Neither touches the network."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel, Field

from databolt.patterns import Pattern, RunContext, pattern


class CountdownParams(BaseModel):
    start: int = Field(10, ge=0, le=10_000)


@pattern(id="example.countdown", version="1.0.0", title="Countdown", icon="timer")
def countdown(params: CountdownParams, ctx: RunContext) -> Iterator[dict[str, Any]]:
    """Yields start, start-1, ..., 1."""
    for n in range(params.start, 0, -1):
        yield {"n": n}


class StaticRowsParams(BaseModel):
    rows: list[dict[str, Any]] = Field(default_factory=lambda: [{"hello": "world"}])


class StaticRows(Pattern):
    """A class-based pattern: emits the rows it is given."""

    id = "example.static_rows"
    version = "1.0.0"
    title = "Static rows"
    Params = StaticRowsParams

    def iter_records(self, params: BaseModel, ctx: RunContext) -> Iterator[dict[str, Any]]:
        assert isinstance(params, StaticRowsParams)
        yield from params.rows


PATTERNS = [StaticRows]
