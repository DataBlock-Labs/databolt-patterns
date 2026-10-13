"""Small patterns the framework tests run."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel, Field

from databolt.patterns import RunContext, SecretRef, StateProposal, pattern
from databolt.patterns.errors import PatternError


class NumbersParams(BaseModel):
    count: int = Field(5, ge=0, description="How many numbers.")
    fail_at: int | None = None
    shout: bool = False


@pattern(id="test.numbers", version="1.2.0", title="Numbers", secrets=[])
def numbers(params: NumbersParams, ctx: RunContext) -> Iterator[dict[str, Any]]:
    """Yields {"n": i}."""
    if params.shout:
        print("hello from a pattern")
    for i in range(params.count):
        if params.fail_at is not None and i == params.fail_at:
            raise PatternError("boom at " + str(i), code="boom")
        ctx.stats.pages = i // 2 + 1
        yield {"n": i}


class FetchParams(BaseModel):
    url: str
    token: SecretRef = "${secrets.token}"


@pattern(id="test.fetch", version="0.1.0", secrets=["token"])
def fetch(params: FetchParams, ctx: RunContext) -> Iterator[dict[str, Any]]:
    token = ctx.render(params.token)
    ctx.log(f"fetching with token {token}")
    yield from ctx.http.get(params.url, headers={"Authorization": f"Bearer {token}"}).json()


class Counter:
    """A tracker that proposes the max `n` seen, unless the run was truncated."""

    def __init__(self) -> None:
        self.max: int | None = None

    def observe(self, record: dict[str, Any]) -> None:
        self.max = record["n"] if self.max is None else max(self.max, record["n"])

    def proposal(self, truncated: bool) -> StateProposal:
        if truncated or self.max is None:
            return StateProposal(None, False, "not advancing")
        return StateProposal({"cursor": self.max}, True)


@pattern(id="test.tracked", version="1.0.0")
def tracked(params: NumbersParams, ctx: RunContext) -> Iterator[dict[str, Any]]:
    ctx.track_state(Counter())
    for i in range(params.count):
        yield {"n": i}


@pattern(id="test.proposes", version="1.0.0")
def proposes(params: NumbersParams, ctx: RunContext) -> Iterator[dict[str, Any]]:
    yield {"n": 1}
    ctx.propose_state({"seen": params.count})
