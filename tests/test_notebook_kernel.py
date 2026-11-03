"""The notebook kernel's line protocol (spec 024), with no container."""

from __future__ import annotations

import io
import json

from databolt.kernel import MAX_LINE, Kernel


def session(lines: list[dict]) -> list[dict]:
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in lines))
    stdout = io.StringIO()
    Kernel(stdin, stdout).serve()
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def test_cells_share_a_namespace_and_return_the_last_value() -> None:
    out = session(
        [
            {"op": "exec", "code": "x = 41\nprint('hi')"},
            {"op": "exec", "code": "x + 1"},
        ]
    )
    assert out[0] == {
        "op": "done",
        "ok": True,
        "stdout": "hi\n",
        "value": None,
        "displays": [],
        "error": None,
    }
    assert out[1]["value"] == "42"


def test_sql_asks_studio_and_displays_a_table() -> None:
    out = session(
        [
            {
                "op": "exec",
                "code": (
                    "t = sql('SELECT 1 AS one', n=2)\n"
                    "display(t, chart={'kind': 'bar', 'x': 'one', 'y': 'one'})\n"
                    "len(t)"
                ),
            },
            {"op": "sql_result", "ok": True, "columns": ["one"], "rows": [[1]]},
        ]
    )
    assert out[0] == {"op": "sql", "sql": "SELECT 1 AS one", "params": {"n": 2}}
    done = out[1]
    assert done["ok"] and done["value"] == "1"
    assert done["displays"] == [
        {"type": "chart", "columns": ["one"], "rows": [[1]], "kind": "bar", "x": "one", "y": "one"}
    ]


def test_a_failed_query_raises_in_the_cell() -> None:
    out = session(
        [
            {"op": "exec", "code": "sql('DROP TABLE x')"},
            {"op": "sql_result", "ok": False, "error": "a query runs exactly one SELECT"},
        ]
    )
    assert not out[1]["ok"] and "exactly one SELECT" in out[1]["error"]


def test_widgets_read_notebook_params() -> None:
    out = session(
        [
            {
                "op": "exec",
                "code": "dbutils.widgets.text('day', '2026-01-01')\ndbutils.widgets.get('day')",
                "params": {"day": "2026-11-03"},
            }
        ]
    )
    assert out[0]["value"] == "'2026-11-03'"


def test_errors_and_bad_messages() -> None:
    out = session([{"op": "exec", "code": "1/0"}, {"op": "nope"}])
    assert not out[0]["ok"] and "ZeroDivisionError" in out[0]["error"]
    assert "unknown op" in out[1]["error"]
    stdin = io.StringIO("not json\n")
    stdout = io.StringIO()
    Kernel(stdin, stdout).serve()
    assert "bad message" in json.loads(stdout.getvalue())["error"]


def test_an_oversized_message_ends_the_session() -> None:
    stdin = io.StringIO("x" * (MAX_LINE + 10) + "\n")
    stdout = io.StringIO()
    Kernel(stdin, stdout).serve()
    assert "8 MiB" in json.loads(stdout.getvalue())["error"]
