"""Where records land: NDJSON (always available), Parquet and DuckDB (optional extras)."""

from __future__ import annotations

import os
from pathlib import Path

from databolt.patterns.errors import ValidationError
from databolt.patterns.sinks.base import FileInfo, MemorySink, Sink
from databolt.patterns.sinks.duckdb import DuckDBSink
from databolt.patterns.sinks.ndjson import NdjsonSink
from databolt.patterns.sinks.parquet import ParquetSink

FORMATS = ("ndjson", "parquet", "duckdb")

__all__ = [
    "FORMATS",
    "DuckDBSink",
    "FileInfo",
    "MemorySink",
    "NdjsonSink",
    "ParquetSink",
    "Sink",
    "sink_for",
]


def sink_for(
    fmt: str, out: str | os.PathLike[str], *, table: str = "records", stem: str | None = None
) -> Sink:
    """A sink writing `fmt` into directory `out` (or to `out` itself if it has a suffix)."""
    p = Path(out)
    if fmt == "ndjson":
        return NdjsonSink(p, stem=stem)
    if fmt == "parquet":
        return ParquetSink(p if p.suffix else p / f"{stem or 'records'}.parquet")
    if fmt == "duckdb":
        return DuckDBSink(p if p.suffix else p / "records.duckdb", table=table)
    raise ValidationError(f"unknown output format {fmt!r}; use one of {', '.join(FORMATS)}")
