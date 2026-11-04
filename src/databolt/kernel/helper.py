"""The `databolt` helpers inside a Python notebook kernel (spec 027).

The Python kernel image loads this module at startup (an IPython startup file),
so cells can call `sql()`, `display()` and `dbutils.widgets` as in spec 024.

`sql()` and the widgets reach Studio through the kernel's stdin channel: they
call `input()` with a reserved prompt, and the bridge answers it. A Jupyter comm
cannot answer while a cell runs, because the kernel reads no shell messages until
the cell ends; stdin is answered during the cell.
"""

from __future__ import annotations

import json
from typing import Any

from databolt.kernel import MAX_ROWS, PARAMS_PROMPT, SQL_PROMPT, TABLE_MIME, Table


def sql(query: str, **params: Any) -> Table:
    """Rows of one read-only SELECT, run by Studio on the project's engine."""
    reply = json.loads(input(SQL_PROMPT + json.dumps({"sql": query, "params": params})))
    if not reply.get("ok"):
        raise RuntimeError(str(reply.get("error", "the query failed")))
    return Table(list(reply.get("columns", [])), list(reply.get("rows", []))[:MAX_ROWS])


def table_bundle(obj: Any, chart: dict[str, str] | None = None) -> dict[str, Any] | None:
    """The table MIME bundle of a Table or a list of dicts, or None."""
    if isinstance(obj, list) and obj and all(isinstance(r, dict) for r in obj):
        cols = list(obj[0])
        obj = Table(cols, [[r.get(c) for c in cols] for r in obj])
    if not isinstance(obj, Table):
        return None
    data: dict[str, Any] = {"columns": obj.columns, "rows": obj.rows[:MAX_ROWS]}
    if chart:
        data["chart"] = {
            "kind": chart.get("kind", "bar"),
            "x": chart.get("x"),
            "y": chart.get("y"),
        }
    return {TABLE_MIME: data, "text/plain": repr(obj)}


def display(obj: Any, chart: dict[str, str] | None = None) -> None:
    """Shows a Table (or a list of dicts) as a table, or as a chart with `chart`."""
    from IPython.display import display as ipy_display

    bundle = table_bundle(obj, chart)
    if bundle is None:
        ipy_display(obj)
    else:
        ipy_display(bundle, raw=True)


class _Widgets:
    """`dbutils.widgets`: the notebook parameters of the running cell."""

    def _values(self) -> dict[str, str]:
        params = json.loads(input(PARAMS_PROMPT))
        return {k: str(v) for k, v in params.items()} if isinstance(params, dict) else {}

    def text(self, name: str, default: str = "", label: str | None = None) -> None:
        self._defaults[name] = default

    def dropdown(
        self, name: str, default: str, choices: list[str], label: str | None = None
    ) -> None:
        self._defaults[name] = default

    combobox = dropdown

    def __init__(self) -> None:
        self._defaults: dict[str, str] = {}

    def get(self, name: str) -> str:
        values = {**self._defaults, **self._values()}
        if name not in values:
            raise KeyError(f"no widget or notebook parameter named {name}")
        return values[name]

    def getAll(self) -> dict[str, str]:
        return {**self._defaults, **self._values()}


class _DbUtils:
    def __init__(self) -> None:
        self.widgets = _Widgets()


dbutils = _DbUtils()

__all__ = ["Table", "dbutils", "display", "sql", "table_bundle"]
