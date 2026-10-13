from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import IO, Any

from databolt.patterns.sinks.base import FileInfo, Sink


def dumps(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)


class NdjsonSink(Sink):
    """One JSON object per line. `out` is a directory (a unique file is created in it)
    or a file path ending in .ndjson/.jsonl. A run with zero rows leaves no file."""

    def __init__(self, out: Path, *, stem: str | None = None, name: str | None = None) -> None:
        # absolute() not resolve(): a caller checks paths are inside the dir it passed, and
        # resolving a symlinked tmp dir would change the prefix.
        out = Path(os.path.abspath(out))
        if out.suffix in (".ndjson", ".jsonl", ".json"):
            self.path = out
        else:
            self.path = out / (name or f"{stem or 'part'}-{uuid.uuid4().hex[:12]}.ndjson")
        self._fh: IO[str] | None = None

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8", newline="\n")

    def write(self, record: dict[str, Any]) -> None:
        if self._fh is None:
            self.open()
        assert self._fh is not None
        self._fh.write(dumps(record))
        self._fh.write("\n")
        self.rows += 1

    def close(self) -> list[FileInfo]:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        if not self.path.exists():
            return []
        if self.rows == 0:
            self.path.unlink(missing_ok=True)
            return []
        return [FileInfo(str(self.path), self.rows, self.path.stat().st_size)]

    def abort(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        self.path.unlink(missing_ok=True)
