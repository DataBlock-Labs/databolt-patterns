"""Studio's notebook kernel (spec 024): Python cells, driven by JSON lines.

Studio runs `python -I -m databolt.kernel` inside a locked container (no network,
read-only filesystem, no secrets) and writes one JSON object per line on stdin:

    {"op": "exec", "code": "...", "params": {...}}

While a cell runs, the kernel may ask Studio for rows; Studio runs them on its
read-only query path and answers on stdin:

    -> {"op": "sql", "sql": "...", "params": {...}}
    <- {"op": "sql_result", "ok": true, "columns": [...], "rows": [[...]]}

When the cell ends, the kernel answers:

    {"op": "done", "ok": true, "stdout": "...", "value": "repr or null",
     "displays": [{"type": "table" | "chart" | "text", ...}], "error": null}

The namespace persists between cells. Helpers: `sql(query, **params)`,
`display(obj, chart=None)` and `dbutils.widgets` (fed from the notebook params).
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import sys
import traceback
from typing import Any, TextIO

MAX_LINE = 8 * 1024 * 1024
MAX_STDOUT = 1024 * 1024
MAX_ROWS = 10_000


class Table:
    """Rows from `sql()`: columns plus row lists. `display(t)` shows it."""

    def __init__(self, columns: list[str], rows: list[list[Any]]) -> None:
        self.columns = columns
        self.rows = rows

    def to_dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, r, strict=False)) for r in self.rows]

    def __len__(self) -> int:
        return len(self.rows)

    def __repr__(self) -> str:
        return (
            f"Table({len(self.rows)} rows x {len(self.columns)} columns: {', '.join(self.columns)})"
        )


class _Widgets:
    """`dbutils.widgets`: notebook parameters, with the defaults the cell gives."""

    def __init__(self, params: dict[str, Any]) -> None:
        self._values = {k: str(v) for k, v in params.items()}

    def text(self, name: str, default: str = "", label: str | None = None) -> None:
        self._values.setdefault(name, default)

    def dropdown(
        self, name: str, default: str, choices: list[str], label: str | None = None
    ) -> None:
        self._values.setdefault(name, default)

    combobox = dropdown

    def get(self, name: str) -> str:
        if name not in self._values:
            raise KeyError(f"no widget or notebook parameter named {name}")
        return self._values[name]

    def getAll(self) -> dict[str, str]:
        return dict(self._values)


class _DbUtils:
    def __init__(self, params: dict[str, Any]) -> None:
        self.widgets = _Widgets(params)


class Kernel:
    """One notebook session: a namespace and the line protocol."""

    def __init__(self, stdin: TextIO, stdout: TextIO) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.ns: dict[str, Any] = {"__name__": "__notebook__"}
        self.displays: list[dict[str, Any]] = []

    def send(self, msg: dict[str, Any]) -> None:
        self.stdout.write(json.dumps(msg, default=str) + "\n")
        self.stdout.flush()

    def read(self) -> dict[str, Any] | None:
        line = self.stdin.readline(MAX_LINE + 1)
        if not line:
            return None
        if len(line) > MAX_LINE:
            raise ValueError("a message is longer than 8 MiB")
        msg = json.loads(line)
        if not isinstance(msg, dict):
            raise ValueError("a message is a JSON object")
        return msg

    def sql(self, query: str, **params: Any) -> Table:
        """Rows of one read-only SELECT, run by Studio on the project's engine."""
        self.send({"op": "sql", "sql": query, "params": params})
        reply = self.read()
        if not reply or reply.get("op") != "sql_result":
            raise RuntimeError("Studio did not answer the sql request")
        if not reply.get("ok"):
            raise RuntimeError(str(reply.get("error", "the query failed")))
        return Table(list(reply.get("columns", [])), list(reply.get("rows", []))[:MAX_ROWS])

    def display(self, obj: Any, chart: dict[str, str] | None = None) -> None:
        """Shows a Table (or a list of dicts) as a table, or as a chart with `chart`."""
        if isinstance(obj, list) and obj and all(isinstance(r, dict) for r in obj):
            cols = list(obj[0])
            obj = Table(cols, [[r.get(c) for c in cols] for r in obj])
        if isinstance(obj, Table):
            out: dict[str, Any] = {
                "type": "table",
                "columns": obj.columns,
                "rows": obj.rows[:MAX_ROWS],
            }
            if chart:
                out = {
                    **out,
                    "type": "chart",
                    "kind": chart.get("kind", "bar"),
                    "x": chart.get("x"),
                    "y": chart.get("y"),
                }
            self.displays.append(out)
        else:
            self.displays.append({"type": "text", "text": repr(obj)})

    def run(self, code: str, params: dict[str, Any]) -> dict[str, Any]:
        self.displays = []
        self.ns.update(sql=self.sql, display=self.display, dbutils=_DbUtils(params))
        buf = io.StringIO()
        value: str | None = None
        try:
            tree = ast.parse(code, "<cell>", "exec")
            tail = tree.body[-1] if tree.body else None
            last = tail if isinstance(tail, ast.Expr) else None
            if last is not None:
                tree.body.pop()
            with contextlib.redirect_stdout(buf):
                exec(compile(tree, "<cell>", "exec"), self.ns)  # noqa: S102 - the sandbox is the boundary
                if last is not None:
                    result = eval(compile(ast.Expression(last.value), "<cell>", "eval"), self.ns)  # noqa: S307
                    if result is not None:
                        value = repr(result)
            ok, error = True, None
        except Exception:
            ok, error = False, traceback.format_exc(limit=5)
        return {
            "op": "done",
            "ok": ok,
            "stdout": buf.getvalue()[:MAX_STDOUT],
            "value": value,
            "displays": self.displays,
            "error": error,
        }

    def serve(self) -> None:
        while True:
            try:
                msg = self.read()
            except (ValueError, json.JSONDecodeError) as e:
                self.send({"op": "done", "ok": False, "error": f"bad message: {e}"})
                return
            if msg is None or msg.get("op") == "close":
                return
            if msg.get("op") != "exec":
                self.send({"op": "done", "ok": False, "error": f"unknown op {msg.get('op')!r}"})
                continue
            params = msg.get("params") or {}
            self.send(
                self.run(str(msg.get("code", "")), params if isinstance(params, dict) else {})
            )


def main() -> None:
    # Cell prints go to the captured buffer; only protocol lines reach the real stdout.
    Kernel(sys.stdin, sys.stdout).serve()
