from __future__ import annotations

from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from databolt.ingestion.api import RestConfig
from tests.api_helpers import run_rest


def cfg(server: HTTPServer, path: str, **extra: object) -> dict[str, object]:
    resource = {"path": path, **extra.pop("resource", {})}  # type: ignore[dict-item]
    return {"client": {"base_url": server.url_for("/"), **extra}, "resource": resource}


def test_single_page(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/rows").respond_with_json([{"a": 1}, {"a": 2}])
    res, rows = run_rest(cfg(httpserver, "rows", paginator="single_page"))
    assert rows == [{"a": 1}, {"a": 2}]
    assert res.pages == 1 and res.paginator_detected == "single_page"


def test_json_link(httpserver: HTTPServer) -> None:
    base = httpserver.url_for("/items")
    httpserver.expect_request("/items", query_string="").respond_with_json(
        {"results": [{"i": 1}], "next": f"{base}?page=2"}
    )
    httpserver.expect_request("/items", query_string="page=2").respond_with_json(
        {"results": [{"i": 2}], "next": "/items?page=3"}  # relative next URL
    )
    httpserver.expect_request("/items", query_string="page=3").respond_with_json(
        {"results": [{"i": 3}], "next": None}
    )
    res, rows = run_rest(
        cfg(
            httpserver,
            "items",
            paginator={"type": "json_link", "next_url_path": "next"},
            resource={"data_selector": "results"},
        )
    )
    assert [r["i"] for r in rows] == [1, 2, 3]
    assert res.pages == 3 and not res.truncated


def test_header_link(httpserver: HTTPServer) -> None:
    url = httpserver.url_for("/repos")
    httpserver.expect_request("/repos", query_string="per_page=2").respond_with_json(
        [{"r": 1}, {"r": 2}], headers={"Link": f'<{url}?per_page=2&page=2>; rel="next"'}
    )
    httpserver.expect_request("/repos", query_string="per_page=2&page=2").respond_with_json(
        [{"r": 3}], headers={"Link": f'<{url}?per_page=2&page=1>; rel="first"'}
    )
    res, rows = run_rest(
        cfg(httpserver, "repos", paginator="header_link", resource={"params": {"per_page": 2}})
    )
    assert [r["r"] for r in rows] == [1, 2, 3]
    assert res.paginator_detected == "header_link"


def test_header_cursor(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/e", query_string="").respond_with_json(
        [{"e": 1}], headers={"X-Next-Cursor": "abc"}
    )
    httpserver.expect_request("/e", query_string="cursor=abc").respond_with_json([{"e": 2}])
    _, rows = run_rest(
        cfg(httpserver, "e", paginator={"type": "header_cursor", "cursor_header": "X-Next-Cursor"})
    )
    assert [r["e"] for r in rows] == [1, 2]


def test_cursor_in_query_and_body(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/q", query_string="").respond_with_json(
        {"data": [{"x": 1}], "meta": {"next": "c2"}}
    )
    httpserver.expect_request("/q", query_string="after=c2").respond_with_json(
        {"data": [{"x": 2}], "meta": {"next": None}}
    )
    _, rows = run_rest(
        cfg(
            httpserver,
            "q",
            paginator={"type": "cursor", "cursor_path": "meta.next", "cursor_param": "after"},
            resource={"data_selector": "data"},
        )
    )
    assert [r["x"] for r in rows] == [1, 2]

    httpserver.clear()
    httpserver.expect_request("/s", method="POST", json={"q": "x"}).respond_with_json(
        {"hits": [{"h": 1}], "next_cursor": "n2"}
    )
    httpserver.expect_request(
        "/s", method="POST", json={"q": "x", "cursor": "n2"}
    ).respond_with_json({"hits": [{"h": 2}], "next_cursor": ""})
    _, rows = run_rest(
        cfg(
            httpserver,
            "s",
            paginator={"type": "cursor", "location": "json"},
            resource={"method": "POST", "json": {"q": "x"}, "data_selector": "hits"},
        )
    )
    assert [r["h"] for r in rows] == [1, 2]


def test_offset_with_base_and_short_page(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/ev", query_string="offset=1&limit=2").respond_with_json(
        {"features": [{"n": 1}, {"n": 2}]}
    )
    httpserver.expect_request("/ev", query_string="offset=3&limit=2").respond_with_json(
        {"features": [{"n": 3}, {"n": 4}]}
    )
    httpserver.expect_request("/ev", query_string="offset=5&limit=2").respond_with_json(
        {"features": [{"n": 5}]}
    )
    res, rows = run_rest(
        cfg(
            httpserver,
            "ev",
            paginator={"type": "offset", "base": 1, "limit": 2},
            resource={"data_selector": "features"},
        )
    )
    assert [r["n"] for r in rows] == [1, 2, 3, 4, 5]
    assert res.pages == 3


def test_offset_total_path(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/t", query_string="offset=0&limit=2").respond_with_json(
        {"total": 4, "items": [{"n": 1}, {"n": 2}]}
    )
    httpserver.expect_request("/t", query_string="offset=2&limit=2").respond_with_json(
        {"total": 4, "items": [{"n": 3}, {"n": 4}]}
    )
    res, rows = run_rest(
        cfg(
            httpserver,
            "t",
            paginator={"type": "offset", "limit": 2, "total_path": "total"},
            resource={"data_selector": "items"},
        )
    )
    assert len(rows) == 4 and res.pages == 2  # no third request for an empty page


def test_page_number(httpserver: HTTPServer) -> None:
    for page, rows in ((1, [{"p": 1}, {"p": 1}]), (2, [{"p": 2}, {"p": 2}]), (3, [])):
        httpserver.expect_request("/pg", query_string=f"page={page}&size=2").respond_with_json(
            {"data": rows}
        )
    res, rows = run_rest(
        cfg(
            httpserver,
            "pg",
            paginator={"type": "page_number", "page_size": 2, "page_size_param": "size"},
            resource={"data_selector": "data"},
        )
    )
    assert len(rows) == 4 and res.pages == 3


def test_page_number_total_pages(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/tp", query_string="page=0").respond_with_json(
        {"pages": 2, "data": [{"p": 0}]}
    )
    httpserver.expect_request("/tp", query_string="page=1").respond_with_json(
        {"pages": 2, "data": [{"p": 1}]}
    )
    res, _ = run_rest(
        cfg(
            httpserver,
            "tp",
            paginator={"type": "page_number", "base_page": 0, "total_path": "pages"},
            resource={"data_selector": "data"},
        )
    )
    assert res.pages == 2


# -- auto-detection ------------------------------------------------------------------------


def test_auto_detects_link_header(httpserver: HTTPServer) -> None:
    url = httpserver.url_for("/a")
    httpserver.expect_request("/a", query_string="").respond_with_json(
        [{"i": 1}], headers={"Link": f'<{url}?page=2>; rel="next"'}
    )
    httpserver.expect_request("/a", query_string="page=2").respond_with_json([{"i": 2}])
    res, rows = run_rest(cfg(httpserver, "a"))
    assert len(rows) == 2 and res.paginator_detected == "header_link"


def test_auto_detects_json_link(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/b", query_string="limit=1").respond_with_json(
        {"results": [{"i": 1}], "next": httpserver.url_for("/b") + "?limit=1&offset=1"}
    )
    httpserver.expect_request("/b", query_string="limit=1&offset=1").respond_with_json(
        {"results": [{"i": 2}], "next": None}
    )
    res, rows = run_rest(cfg(httpserver, "b", resource={"params": {"limit": 1}}))
    assert len(rows) == 2
    assert res.paginator_detected == "json_link"
    assert res.warnings == ["data_selector not set; using 'results'"]


def test_auto_detects_odata_next_link(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/o", query_string="").respond_with_json(
        {"value": [{"i": 1}], "@odata.nextLink": httpserver.url_for("/o") + "?$skip=1"}
    )
    httpserver.expect_request("/o", query_string="$skip=1").respond_with_json({"value": []})
    res, rows = run_rest(cfg(httpserver, "o", resource={"data_selector": "value"}))
    assert len(rows) == 1 and res.paginator_detected == "json_link"


def test_auto_detects_cursor(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/c", query_string="").respond_with_json(
        {"data": [{"i": 1}], "next_cursor": "zz"}
    )
    httpserver.expect_request("/c", query_string="cursor=zz").respond_with_json(
        {"data": [{"i": 2}], "next_cursor": None}
    )
    res, rows = run_rest(cfg(httpserver, "c", resource={"data_selector": "data"}))
    assert len(rows) == 2 and res.paginator_detected == "cursor"
    assert any("auto-detected cursor" in w for w in res.warnings)


def test_auto_falls_back_to_single_page(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/d").respond_with_json({"id": 1, "name": "only"})
    res, rows = run_rest(cfg(httpserver, "d"))
    assert rows == [{"id": 1, "name": "only"}]
    assert res.paginator_detected == "single_page" and res.pages == 1


def test_auto_ignores_non_url_next(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/n").respond_with_json({"items": [{"i": 1}], "next": "token-x"})
    res, _ = run_rest(cfg(httpserver, "n", resource={"data_selector": "items"}))
    assert res.paginator_detected == "single_page"


# -- limits and loops ----------------------------------------------------------------------


def _endless(request: Request) -> Response:
    import json

    page = int(request.args.get("page", "1"))
    body = {"items": [{"page": page}], "next": f"/inf?page={page + 1}"}
    return Response(json.dumps(body), content_type="application/json")


def test_max_pages_truncates(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/inf").respond_with_handler(_endless)
    params = cfg(httpserver, "inf", resource={"data_selector": "items"})
    params["max_pages"] = 3
    res, rows = run_rest(params)
    assert res.pages == 3 and res.requests == 3 and res.truncated
    assert [r["page"] for r in rows] == [1, 2, 3]


def test_max_records_stops_without_extra_requests(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/inf").respond_with_handler(_endless)
    params = cfg(httpserver, "inf", resource={"data_selector": "items"})
    params["max_records"] = 2
    res, rows = run_rest(params)
    assert len(rows) == 2 and res.truncated and res.requests == 2


def test_repeated_page_stops_with_warning(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/same").respond_with_json(
        {"items": [{"i": 1}], "next": httpserver.url_for("/same")}
    )
    res, rows = run_rest(cfg(httpserver, "same", resource={"data_selector": "items"}))
    assert res.pages == 1 and len(rows) == 1
    assert any("already fetched" in w for w in res.warnings)


def test_flat_and_endpoint_forms_fold_into_nested() -> None:
    flat = RestConfig.model_validate(
        {"base_url": "https://x.test", "path": "p", "paginator": "offset", "max_pages": 2}
    )
    assert flat.client.base_url == "https://x.test" and flat.resource.path == "p"
    assert flat.client.paginator.type == "offset"
    dlt = RestConfig.model_validate(
        {"client": {"base_url": "https://x.test"}, "endpoint": {"path": "q"}}
    )
    assert dlt.resource.path == "q"
