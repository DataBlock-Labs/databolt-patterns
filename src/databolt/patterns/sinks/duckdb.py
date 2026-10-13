from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any

from databolt.patterns.errors import ValidationError
from databolt.patterns.sinks.base import FileInfo, Sink
from databolt.patterns.sinks.ndjson import NdjsonSink

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _duckdb() -> Any:
    try:
        return importlib.import_module("duckdb")
    except ImportError:
        raise ValidationError(
            "DuckDB output needs duckdb: pip install 'databolt-standard-patterns[duckdb]'"
        ) from None


class DuckDBSink(Sink):
    """Stages NDJSON next to the database, then loads it into `table` in one statement.

    mode="replace" recreates the table; mode="append" inserts by column name (creating
    the table on first use).
    """

    def __init__(self, path: Path, *, table: str = "records", mode: str = "replace") -> None:
        if not _IDENT.match(table):
            raise ValidationError(f"invalid DuckDB table name {table!r}")
        if mode not in ("replace", "append"):
            raise ValidationError("DuckDB mode must be 'replace' or 'append'")
        _duckdb()
        self.path = path
        self.table = table
        self.mode = mode
        self._stage = NdjsonSink(path.parent / f".{path.stem}-stage.ndjson")

    def write(self, record: dict[str, Any]) -> None:
        self._stage.write(record)
        self.rows += 1

    def close(self) -> list[FileInfo]:
        staged = self._stage.close()
        if not staged:
            return []
        duckdb = _duckdb()
        src = staged[0].path.replace("'", "''")
        read = f"read_json_auto('{src}', format = 'newline_delimited')"
        # self.table is validated against _IDENT in __init__; src is quote-escaped.
        try:
            con = duckdb.connect(str(self.path))
            try:
                if self.mode == "replace":
                    con.execute(f"CREATE OR REPLACE TABLE {self.table} AS SELECT * FROM {read}")  # noqa: S608
                else:
                    con.execute(
                        f"CREATE TABLE IF NOT EXISTS {self.table} AS SELECT * FROM {read} LIMIT 0"  # noqa: S608
                    )
                    con.execute(f"INSERT INTO {self.table} BY NAME SELECT * FROM {read}")  # noqa: S608
            finally:
                con.close()
        finally:
            Path(staged[0].path).unlink(missing_ok=True)
        return [FileInfo(str(self.path.resolve()), self.rows, self.path.stat().st_size)]

    def abort(self) -> None:
        self._stage.abort()
