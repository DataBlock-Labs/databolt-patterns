from __future__ import annotations

import json
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer

from databolt.ingestion import api
from databolt.patterns import EgressPolicy
from databolt.patterns.errors import HttpError
from tests.api_helpers import LOCAL, run_rest


def test_response_action_ignores_404(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/missing").respond_with_data("no data", status=404)
    res, rows = run_rest(
        {
            "client": {"base_url": httpserver.url_for("/")},
            "resource": {"path": "missing", "response_actions": [{"status_code": 404}]},
        }
    )
    assert rows == [] and res.ok and res.pages == 1


def test_response_action_content_match(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/x").respond_with_data("rate plan exceeded", status=403)
    p = {
        "client": {"base_url": httpserver.url_for("/")},
        "resource": {
            "path": "x",
            "response_actions": [{"status_code": 403, "content": "something else"}],
        },
    }
    with pytest.raises(HttpError):
        run_rest(p)


def test_post_with_json_body(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/search", method="POST", json={"q": "bolt"}).respond_with_json(
        {"hits": [{"id": 1}]}
    )
    _, rows = run_rest(
        {
            "client": {"base_url": httpserver.url_for("/"), "paginator": "single_page"},
            "resource": {
                "path": "search",
                "method": "POST",
                "json": {"q": "bolt"},
                "data_selector": "hits",
            },
        }
    )
    assert rows == [{"id": 1}]


def test_retries_inside_a_paginated_run(httpserver: HTTPServer) -> None:
    httpserver.expect_ordered_request("/r").respond_with_data("", status=503)
    httpserver.expect_ordered_request("/r").respond_with_data(
        "", status=429, headers={"Retry-After": "0"}
    )
    httpserver.expect_ordered_request("/r").respond_with_json([{"ok": 1}])
    res, rows = run_rest(
        {"client": {"base_url": httpserver.url_for("/")}, "resource": {"path": "r"}}
    )
    assert rows == [{"ok": 1}] and res.retries == 2 and res.requests == 3


def test_functional_api_and_sinks(httpserver: HTTPServer, tmp_path: Path) -> None:
    httpserver.expect_request("/p").respond_with_json({"results": [{"n": 1}, {"n": 2}]})
    src = api.rest(
        base_url=httpserver.url_for("/"),
        path="p",
        data_selector="results",
        paginator="single_page",
        auth=api.auth.bearer(),
        secrets={"token": "t"},
        egress=LOCAL,
    )
    assert list(src.iter_records()) == [{"n": 1}, {"n": 2}]
    res = src.to_ndjson(tmp_path)
    assert res.rows == 2 and res.source_label == httpserver.url_for("/p")
    pytest.importorskip("pyarrow")
    assert src.to_parquet(tmp_path / "p.parquet").files[0].rows == 2


def test_class_form(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/c").respond_with_json([{"a": 1}])
    cfg = api.RestConfig.model_validate(
        {"client": {"base_url": httpserver.url_for("/")}, "resource": {"path": "c"}}
    )
    bound = api.RestApiPattern(cfg).bind(egress=LOCAL)
    assert list(bound.iter_records()) == [{"a": 1}]


def test_rest_is_egress_guarded_by_default(httpserver: HTTPServer) -> None:
    from databolt.patterns.errors import EgressBlocked

    src = api.rest(base_url=httpserver.url_for("/"), path="x")
    with pytest.raises(EgressBlocked):
        list(src.iter_records())
    assert len(httpserver.log) == 0


# -- http_get --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "body", "ctype", "expected"),
    [
        ("/a.csv", "a,b\n1,x\n2,y\n", "text/csv", [{"a": "1", "b": "x"}, {"a": "2", "b": "y"}]),
        ("/a.tsv", "a\tb\n1\tx\n", "text/plain", [{"a": "1", "b": "x"}]),
        ("/a", '{"rows": [{"k": 1}]}', "application/json", [{"rows": [{"k": 1}]}]),
        ("/a.ndjson", '{"k":1}\n\n{"k":2}\n', "application/octet-stream", [{"k": 1}, {"k": 2}]),
    ],
)
def test_http_get_formats(
    httpserver: HTTPServer, path: str, body: str, ctype: str, expected: list[dict[str, object]]
) -> None:
    httpserver.expect_request(path).respond_with_data(body, content_type=ctype)
    src = api.http_get(httpserver.url_for(path), egress=LOCAL)
    assert list(src.iter_records()) == expected
    assert src.last_result is not None and src.last_result.paginator_detected == "single_page"


def test_http_get_selector_and_auth(httpserver: HTTPServer) -> None:
    httpserver.expect_request(
        "/d.json", headers={"Authorization": "Bearer zz-top"}
    ).respond_with_json({"rows": [{"k": 1}, {"k": 2}]})
    src = api.http_get(
        httpserver.url_for("/d.json"),
        data_selector="rows",
        auth=api.auth.bearer(),
        secrets={"token": "zz-top"},
        egress=EgressPolicy(allow_private=True, trust_env=False),
    )
    assert [r["k"] for r in src.iter_records()] == [1, 2]


def test_http_get_bad_ndjson(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/bad.ndjson").respond_with_data('{"a":1}\nnope\n')
    with pytest.raises(Exception, match="line 2"):
        list(api.http_get(httpserver.url_for("/bad.ndjson"), egress=LOCAL).iter_records())


def test_examples_validate() -> None:
    import yaml

    root = Path(__file__).resolve().parents[1] / "examples"
    for name in ("pokeapi.yaml", "usgs_earthquakes.yaml", "github_repos.yaml"):
        cfg = yaml.safe_load((root / name).read_text())
        p = api.RestApiPattern().parse_params(cfg)
        assert json.dumps(p.model_dump(by_alias=True))
