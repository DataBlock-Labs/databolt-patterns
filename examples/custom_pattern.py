"""A custom Python pattern: the top Hacker News stories.

Run it by path (the CLI hashes the file you name and runs it):

    databolt-patterns run examples/custom_pattern.py --param limit=20 --out out/hn

Or over the runner protocol, which only executes the file if its sha256 matches:

    {"protocol": 1, "op": "run",
     "pattern": {"file": "/abs/examples/custom_pattern.py", "entry": "hn_top_stories",
                 "sha256": "<sha256 of the file>"}, ...}
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel, Field

from databolt.patterns import RunContext, pattern

API = "https://hacker-news.firebaseio.com/v0"


class Params(BaseModel):
    limit: int = Field(30, ge=1, le=500, description="How many top stories to fetch.")
    min_score: int = Field(0, ge=0, description="Skip stories below this score.")


@pattern(
    id="example.hn_top_stories",
    version="1.0.0",
    title="Hacker News top stories",
    icon="newspaper",
    capabilities={"pagination": False, "incremental": False},
    examples=[{"title": "Top 20", "params": {"limit": 20}}],
)
def hn_top_stories(params: Params, ctx: RunContext) -> Iterator[dict[str, Any]]:
    """Yields one record per story. All requests go through ctx.http, the egress-guarded,
    retrying client, so the run is subject to the same limits as a standard pattern."""
    ids = ctx.http.get(f"{API}/topstories.json").json()[: params.limit]
    ctx.stats.pages = 1
    for n, story_id in enumerate(ids, 1):
        item = ctx.http.get(f"{API}/item/{story_id}.json").json() or {}
        if item.get("score", 0) >= params.min_score:
            yield {
                "id": item.get("id"),
                "title": item.get("title"),
                "url": item.get("url"),
                "score": item.get("score"),
                "by": item.get("by"),
                "time": item.get("time"),
            }
        if n % 10 == 0:
            ctx.log(f"fetched {n}/{len(ids)} stories")
