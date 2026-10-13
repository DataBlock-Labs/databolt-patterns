"""Opt-in tests against real public APIs: `pytest -m live`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from databolt.ingestion import api
from databolt.patterns import run
from databolt.patterns.sinks import MemorySink

pytestmark = pytest.mark.live
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def load(name: str) -> dict[str, object]:
    data = yaml.safe_load((EXAMPLES / name).read_text())
    assert isinstance(data, dict)
    return data


def test_pokeapi_auto_pagination() -> None:
    res = run(api.RestApiPattern(), load("pokeapi.yaml"), sink=MemorySink())
    print(f"pokeapi: rows={res.rows} pages={res.pages} paginator={res.paginator_detected}")
    assert res.rows == 300
    assert res.pages == 3
    assert res.paginator_detected == "json_link"
    assert res.truncated


def test_usgs_incremental_twice() -> None:
    cfg = load("usgs_earthquakes.yaml")
    start = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cfg["resource"]["incremental"]["initial_value"] = start  # type: ignore[index]
    first = run(api.RestApiPattern(), cfg, sink=MemorySink())
    print(f"usgs run 1: rows={first.rows} state={first.state}")
    assert first.rows > 0
    assert first.state_changed
    second = run(api.RestApiPattern(), cfg, state=first.state, sink=MemorySink())
    print(f"usgs run 2: rows={second.rows} state={second.state}")
    # Only events newer than the committed cursor (usually none within seconds).
    assert second.rows <= 3
    assert second.state["cursor"] >= first.state["cursor"]  # type: ignore[index]
