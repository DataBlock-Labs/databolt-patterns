from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from databolt.patterns.errors import ValidationError
from databolt.patterns.sinks.base import FileInfo, Sink


def _pyarrow() -> tuple[Any, Any]:
    try:
        pa = importlib.import_module("pyarrow")
        pq = importlib.import_module("pyarrow.parquet")
    except ImportError:
        raise ValidationError(
            "Parquet output needs pyarrow: pip install 'databolt-standard-patterns[parquet]'"
        ) from None
    return pa, pq


class ParquetSink(Sink):
    """Buffers records, then writes one Parquet file (schema inferred by pyarrow)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._rows: list[dict[str, Any]] = []
        _pyarrow()  # fail fast, before any request is made

    def write(self, record: dict[str, Any]) -> None:
        self._rows.append(record)
        self.rows += 1

    def close(self) -> list[FileInfo]:
        if not self._rows:
            return []
        pa, pq = _pyarrow()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".partial")
        try:
            pq.write_table(pa.Table.from_pylist(self._rows), tmp)
            tmp.replace(self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return [FileInfo(str(self.path.resolve()), self.rows, self.path.stat().st_size)]

    def abort(self) -> None:
        self._rows.clear()
