"""The Spark worker protocol, against a fake session: no Spark installed."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest

from databolt.spark import SparkMissing, Worker, open_session

TOKEN = "dapi-very-secret-token"


class Field:
    def __init__(self, name: str) -> None:
        self.name = name
        self.nullable = True
        self.dataType = self

    def simpleString(self) -> str:
        return "bigint"


class Frame:
    def __init__(self, spark: FakeSpark, name: str, rows: list[tuple[Any, ...]]) -> None:
        self.spark, self.name, self.rows = spark, name, rows
        self.columns = ["id", "v"]
        self.schema = self
        self.fields = [Field("id"), Field("v")]
        self.write = self
        self.fmt = ""

    def limit(self, n: int) -> Frame:
        return Frame(self.spark, self.name, self.rows[:n])

    def collect(self) -> list[tuple[Any, ...]]:
        return self.rows

    def count(self) -> int:
        return len(self.rows)

    def mode(self, _m: str) -> Frame:
        return self

    def format(self, f: str) -> Frame:
        self.fmt = f
        return self

    def saveAsTable(self, name: str) -> None:
        self.spark.tables[name] = self.rows
        self.spark.formats[name] = self.fmt


class FakeSpark:
    version = "4.1.0-fake"

    def __init__(self) -> None:
        self.tables: dict[str, list[tuple[Any, ...]]] = {"`s`.`src`": [(1, "a"), (2, "b")]}
        self.formats: dict[str, str] = {}
        self.statements: list[str] = []
        self.stopped = False

    def sql(self, q: str) -> Frame:
        self.statements.append(q)
        if "fail" in q:
            raise RuntimeError(f"bad query; token {TOKEN} in the error")
        return Frame(self, "q", [(1, "a"), (2, "b"), (3, "c")])

    def table(self, name: str) -> Frame:
        if name not in self.tables:
            raise RuntimeError(f"[TABLE_OR_VIEW_NOT_FOUND] {name}")
        return Frame(self, name, self.tables[name])

    def stop(self) -> None:
        self.stopped = True


def run(lines: list[dict[str, Any]], spark: FakeSpark) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    def opener(spec: dict[str, Any]) -> FakeSpark:
        seen.append(spec)
        return spark

    out = io.StringIO()
    Worker(opener).serve(io.StringIO("".join(json.dumps(x) + "\n" for x in lines)), out)
    return [json.loads(x) for x in out.getvalue().splitlines()]


def test_sql_rows_columns_and_execute() -> None:
    spark = FakeSpark()
    open_ = {"op": "open", "session": {"kind": "connect", "remote": "sc://localhost:15002"}}
    got = run(
        [
            open_,
            {"op": "sql", "sql": "SELECT 1", "limit": 2},
            {"op": "execute", "sql": "CREATE TABLE x"},
            {"op": "columns", "table": "`s`.`src`"},
            {"op": "columns", "table": "`s`.`missing`"},
            {"op": "close"},
        ],
        spark,
    )
    assert got[0] == {"ok": True, "version": "4.1.0-fake"}
    assert got[1] == {"ok": True, "columns": ["id", "v"], "rows": [[1, "a"], [2, "b"]]}
    assert got[2] == {"ok": True}
    assert [c["name"] for c in got[3]["columns"]] == ["id", "v"]
    assert got[4] == {"ok": True, "columns": []}
    assert got[5] == {"ok": True} and spark.stopped


def test_a_failure_is_an_answer_and_masks_the_token() -> None:
    spark = FakeSpark()
    got = run(
        [
            {"op": "sql", "sql": "SELECT 1"},
            {"op": "open", "session": {"kind": "connect", "remote": "sc://h:1", "token": TOKEN}},
            {"op": "sql", "sql": "fail now", "limit": 1},
            {"op": "nope"},
        ],
        spark,
    )
    assert got[0]["ok"] is False and "open a session first" in got[0]["error"]
    assert got[2]["ok"] is False
    assert TOKEN not in got[2]["error"] and "***" in got[2]["error"]
    assert got[3]["ok"] is False and "unknown op" in got[3]["error"]


def test_a_recipe_runs_only_when_its_bytes_match(tmp_path: Path) -> None:
    f = tmp_path / "r.py"
    f.write_text("def build(spark, inputs, params):\n    return inputs['src']\n")
    sha = hashlib.sha256(f.read_bytes()).hexdigest()
    spark = FakeSpark()
    base = {"op": "recipe", "file": str(f), "function": "build", "inputs": {"src": "`s`.`src`"}}
    got = run(
        [
            {"op": "open", "session": {"kind": "connect", "remote": "sc://h:1"}},
            {**base, "sha256": sha, "output": "`s`.`out`"},
            {**base, "sha256": "0" * 64, "output": "`s`.`out2`"},
        ],
        spark,
    )
    assert got[1] == {"ok": True, "rows": 2}
    assert spark.formats["`s`.`out`"] == "delta"
    assert got[2]["ok"] is False and "changed since Studio pinned it" in got[2]["error"]
    assert "`s`.`out2`" not in spark.tables


def test_open_session_refuses_a_bad_remote_and_names_the_extra() -> None:
    with pytest.raises(ValueError, match="sc://"):
        open_session({"kind": "connect", "remote": "http://x"})
    with pytest.raises(ValueError, match="unknown Spark session kind"):
        open_session({"kind": "yarn"})
    try:
        import pyspark  # noqa: F401
    except ImportError:
        with pytest.raises(SparkMissing, match="`spark` extra"):
            open_session({"kind": "connect", "remote": "sc://localhost:15002"})
