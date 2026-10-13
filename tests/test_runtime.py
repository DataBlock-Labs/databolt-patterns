from __future__ import annotations

import io
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer

from databolt.patterns import EgressPolicy, Limits, Registry, run
from databolt.patterns.errors import EgressBlocked, PatternError, ValidationError
from databolt.patterns.runtime import BoundPattern, error_dict
from databolt.patterns.sinks import NdjsonSink, sink_for
from tests import fixture_patterns as fp

PRIVATE_OK = EgressPolicy(allow_private=True, trust_env=False)


def test_run_into_ndjson(tmp_path: Path) -> None:
    res = run(fp.numbers, {"count": 3}, sink=NdjsonSink(tmp_path))
    assert res.ok and res.rows == 3 and not res.truncated
    assert len(res.files) == 1
    assert Path(res.files[0].path).read_text().splitlines() == ['{"n":0}', '{"n":1}', '{"n":2}']
    assert res.files[0].rows == 3 and res.files[0].bytes > 0
    assert res.pattern == {"id": "test.numbers", "version": "1.2.0"}


def test_zero_rows_leaves_no_file(tmp_path: Path) -> None:
    res = run(fp.numbers, {"count": 0}, sink=NdjsonSink(tmp_path))
    assert res.rows == 0 and res.files == []
    assert list(tmp_path.iterdir()) == []


def test_failure_deletes_partial_output(tmp_path: Path) -> None:
    with pytest.raises(PatternError, match="boom"):
        run(fp.numbers, {"count": 5, "fail_at": 3}, sink=NdjsonSink(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_invalid_params() -> None:
    with pytest.raises(ValidationError, match=r"params\.count"):
        run(fp.numbers, {"count": -1})
    with pytest.raises(ValidationError):
        run(fp.numbers, {"count": "many"})


def test_max_records_truncates() -> None:
    res = run(fp.numbers, {"count": 10}, limits=Limits(max_records=4))
    assert res.rows == 4 and res.truncated


def test_max_records_exactly_at_the_end_is_not_truncated() -> None:
    res = run(fp.numbers, {"count": 4}, limits=Limits(max_records=4))
    assert res.rows == 4 and not res.truncated


def test_tracker_proposes_state_after_landing() -> None:
    res = run(fp.tracked, {"count": 3}, state={"cursor": -1})
    assert res.state == {"cursor": 2} and res.state_changed


def test_tracker_sees_only_landed_records() -> None:
    res = run(fp.tracked, {"count": 10}, state={"cursor": -1}, limits=Limits(max_records=3))
    # truncated -> the fixture tracker refuses to advance and warns
    assert res.truncated and not res.state_changed and res.state == {"cursor": -1}
    assert "not advancing" in res.warnings


def test_explicit_state_proposal() -> None:
    res = run(fp.proposes, {"count": 7}, state=None)
    assert res.state == {"seen": 7} and res.state_changed


def test_preview_returns_records_and_never_changes_state() -> None:
    res = run(fp.tracked, {"count": 80}, state={"cursor": 1}, preview=True)
    assert res.records is not None and len(res.records) == 50
    assert res.truncated and res.state == {"cursor": 1} and not res.state_changed
    assert res.files == []


def test_missing_secret_is_a_validation_error() -> None:
    with pytest.raises(ValidationError, match="missing secret"):
        run(fp.fetch, {"url": "https://example.com"})


def test_http_pattern_with_secret(httpserver: HTTPServer) -> None:
    httpserver.expect_request(
        "/rows", headers={"Authorization": "Bearer tok-123"}
    ).respond_with_json([{"a": 1}, {"a": 2}])
    log = io.StringIO()
    res = run(
        fp.fetch,
        {"url": httpserver.url_for("/rows")},
        secrets={"token": "tok-123"},
        egress=PRIVATE_OK,
        log_stream=log,
    )
    assert res.rows == 2 and res.requests == 1
    assert "tok-123" not in log.getvalue() and "***" in log.getvalue()


def test_egress_is_guarded_by_default(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/rows").respond_with_json([])
    with pytest.raises(EgressBlocked):
        run(fp.fetch, {"url": httpserver.url_for("/rows")}, secrets={"token": "t"})


def test_budget_exhaustion_truncates_with_warning(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/rows").respond_with_json([{"a": 1}] * 50)
    res = run(
        fp.fetch,
        {"url": httpserver.url_for("/rows")},
        secrets={"token": "tok"},
        egress=PRIVATE_OK,
        limits=Limits(max_bytes=10),
    )
    assert res.truncated and res.rows == 0
    assert any("byte budget" in w for w in res.warnings)


def test_error_dict_redacts() -> None:
    from databolt.patterns.redact import Redactor

    e = PatternError("token sk-live-abc leaked", code="x", http_status=401)
    d = error_dict(e, Redactor(["sk-live-abc"]))
    assert d == {"code": "x", "message": "token *** leaked", "http_status": 401, "retryable": False}
    assert error_dict(RuntimeError("bad"))["code"] == "internal_error"


def test_bound_pattern_api(tmp_path: Path) -> None:
    reg = Registry()
    p = reg.register(fp.tracked)
    bound = BoundPattern(p, {"count": 3})
    assert [r["n"] for r in bound.iter_records()] == [0, 1, 2]
    res = bound.to_ndjson(tmp_path / "out.ndjson")
    assert res.files[0].path.endswith("out.ndjson")
    assert bound.commit() == {"cursor": 2}
    assert bound.preview().records == [{"n": 0}, {"n": 1}, {"n": 2}]


def test_parquet_and_duckdb_sinks(tmp_path: Path) -> None:
    pq = pytest.importorskip("pyarrow.parquet")
    duckdb = pytest.importorskip("duckdb")
    bound = BoundPattern(Registry().register(fp.numbers), {"count": 4})
    res = bound.to_parquet(tmp_path / "n.parquet")
    assert pq.read_table(res.files[0].path).num_rows == 4
    res = bound.to_duckdb(tmp_path / "n.duckdb", "nums")
    bound.to_duckdb(tmp_path / "n.duckdb", "nums", mode="append")
    con = duckdb.connect(res.files[0].path)
    assert con.execute("select count(*), max(n) from nums").fetchone() == (8, 3)
    con.close()
    assert sorted(f.name for f in tmp_path.iterdir()) == ["n.duckdb", "n.parquet"]


def test_sink_for_rejects_unknown_format(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        sink_for("xlsx", tmp_path)
