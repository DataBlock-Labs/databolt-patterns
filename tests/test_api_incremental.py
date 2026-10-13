from __future__ import annotations

import json
from datetime import datetime, timezone

from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from databolt.ingestion.api.incremental import Incremental, IncrementalConfig, format_iso
from databolt.patterns import Limits
from tests.api_helpers import run_rest

T0 = 1_760_000_000_000  # epoch ms


def ms_iso(ms: int) -> str:
    return format_iso(datetime.fromtimestamp(ms / 1000, tz=timezone.utc))


def quake_server(server: HTTPServer, events: list[int]) -> None:
    """A tiny FDSN-like endpoint: filters by starttime (inclusive), offset is 1-based."""

    def handler(request: Request) -> Response:
        start = request.args.get("starttime")
        offset = int(request.args.get("offset", "1"))
        limit = int(request.args.get("limit", "1000"))
        times = sorted(events)
        if start:
            s = datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp() * 1000
            times = [t for t in times if t >= s]
        page = times[offset - 1 : offset - 1 + limit]
        body = {"features": [{"id": f"ev{t}", "properties": {"time": t}} for t in page]}
        return Response(json.dumps(body), content_type="application/json")

    server.expect_request("/query").respond_with_handler(handler)


def usgs(server: HTTPServer, **inc: object) -> dict[str, object]:
    return {
        "client": {
            "base_url": server.url_for("/"),
            "paginator": {"type": "offset", "base": 1, "limit": 2},
        },
        "resource": {
            "path": "query",
            "params": {"format": "geojson", "orderby": "time-asc"},
            "data_selector": "features",
            "incremental": {
                "param": "starttime",
                "cursor_path": "properties.time",
                "initial_value": ms_iso(T0),
                "transform": "epoch_ms_to_iso",
                "order": "asc",
                **inc,
            },
        },
    }


def test_advance_then_only_new_rows(httpserver: HTTPServer) -> None:
    events = [T0 + i * 1000 for i in range(5)]
    quake_server(httpserver, events)
    res, rows = run_rest(usgs(httpserver))
    assert len(rows) == 5 and res.pages == 3
    assert res.state == {"cursor": ms_iso(T0 + 4000), "cursor_path": "properties.time"}
    assert res.state_changed
    assert httpserver.log[0][0].args["starttime"] == ms_iso(T0)

    # second run, nothing new: the inclusive boundary event is dropped (range_start open)
    res2, rows2 = run_rest(usgs(httpserver), state=res.state)
    assert rows2 == [] and not res2.state_changed and res2.state == res.state

    # new events arrive; only they land
    events += [T0 + 9000, T0 + 9500]
    res3, rows3 = run_rest(usgs(httpserver), state=res.state)
    assert [r["properties"]["time"] for r in rows3] == [T0 + 9000, T0 + 9500]
    assert res3.state["cursor"] == ms_iso(T0 + 9500)


def test_closed_range_keeps_boundary(httpserver: HTTPServer) -> None:
    quake_server(httpserver, [T0, T0 + 1000])
    state = {"cursor": ms_iso(T0 + 1000), "cursor_path": "properties.time"}
    _, rows = run_rest(usgs(httpserver, range_start="closed"), state=state)
    assert [r["properties"]["time"] for r in rows] == [T0 + 1000]


def test_lookback_rereads_a_window(httpserver: HTTPServer) -> None:
    quake_server(httpserver, [T0, T0 + 1000, T0 + 2000])
    state = {"cursor": ms_iso(T0 + 2000), "cursor_path": "properties.time"}
    _, rows = run_rest(usgs(httpserver, lookback=1.5), state=state)
    assert [r["properties"]["time"] for r in rows] == [T0 + 1000, T0 + 2000]
    assert httpserver.log[0][0].args["starttime"] == ms_iso(T0 + 500)


def test_truncated_ascending_run_advances(httpserver: HTTPServer) -> None:
    quake_server(httpserver, [T0 + i * 1000 for i in range(6)])
    p = usgs(httpserver)
    p["max_pages"] = 1
    res, rows = run_rest(p)
    assert res.truncated and len(rows) == 2
    assert res.state_changed and res.state["cursor"] == ms_iso(T0 + 1000)


def test_truncated_unordered_run_does_not_advance(httpserver: HTTPServer) -> None:
    quake_server(httpserver, [T0 + i * 1000 for i in range(6)])
    p = usgs(httpserver, order="desc")
    p["max_pages"] = 1
    prior = {"cursor": ms_iso(T0 - 5000), "cursor_path": "properties.time"}
    res, rows = run_rest(p, state=prior)
    assert res.truncated and len(rows) == 2
    assert not res.state_changed and res.state == prior
    assert any("not advanced" in w for w in res.warnings)


def test_truncated_by_max_records_unordered(httpserver: HTTPServer) -> None:
    quake_server(httpserver, [T0 + i * 1000 for i in range(6)])
    res, _ = run_rest(usgs(httpserver, order="unordered"), limits=Limits(max_records=3))
    assert res.truncated and not res.state_changed


def test_cursor_path_change_restarts(httpserver: HTTPServer) -> None:
    quake_server(httpserver, [T0, T0 + 1000])
    state = {"cursor": ms_iso(T0 + 1000), "cursor_path": "properties.updated"}
    res, rows = run_rest(usgs(httpserver), state=state)
    assert len(rows) == 2
    assert any("starting from initial_value" in w for w in res.warnings)


def test_state_template_in_params(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/log", query_string="since=5").respond_with_json(
        [{"seq": 6}, {"seq": 7}]
    )
    p = {
        "client": {"base_url": httpserver.url_for("/")},
        "resource": {
            "path": "log",
            "params": {"since": "${state.cursor}"},
            "incremental": {"cursor_path": "seq", "initial_value": 0},
        },
    }
    res, rows = run_rest(p, state={"cursor": 5, "cursor_path": "seq"})
    assert len(rows) == 2 and res.state == {"cursor": 7, "cursor_path": "seq"}


def test_tracker_unit_rules() -> None:
    inc = Incremental(IncrementalConfig(cursor_path="t", initial_value=10), None)
    assert inc.param_value() == 10
    assert not inc.keep({"t": 9}) and inc.keep({"t": 10}) and inc.keep({"x": 1})
    inc.observe({"t": 12})
    inc.observe({"t": 11})
    p = inc.proposal(truncated=False)
    assert p.changed and p.state == {"cursor": 12, "cursor_path": "t"}

    iso = Incremental(
        IncrementalConfig(cursor_path="ts", transform="iso8601"),
        {"cursor": "2026-01-01T00:00:00Z"},
    )
    assert not iso.keep({"ts": "2026-01-01T00:00:00+00:00"})  # open range, equal
    assert iso.keep({"ts": "2026-01-01T00:00:01Z"})
    iso.observe({"ts": "2025-12-31T00:00:00Z"})
    assert not iso.proposal(truncated=False).changed  # never moves backwards

    secs = Incremental(IncrementalConfig(cursor_path="t", transform="epoch_s_to_iso"), None)
    secs.observe({"t": 1_760_000_000})
    assert secs.proposal(False).state["cursor"] == "2025-10-09T08:53:20.000Z"
