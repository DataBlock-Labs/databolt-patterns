from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class FileInfo:
    path: str
    rows: int
    bytes: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Sink(ABC):
    """Receives records one at a time. `close` returns the files written; `abort`
    deletes anything partial so a failed run leaves nothing behind."""

    rows = 0

    def open(self) -> None:  # noqa: B027 - optional hook
        """Called once before the first record."""

    @abstractmethod
    def write(self, record: dict[str, Any]) -> None: ...

    @abstractmethod
    def close(self) -> list[FileInfo]: ...

    @abstractmethod
    def abort(self) -> None: ...


class MemorySink(Sink):
    """Keeps records in memory (preview, tests, `iter_records`)."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write(self, record: dict[str, Any]) -> None:
        self.records.append(record)
        self.rows += 1

    def close(self) -> list[FileInfo]:
        return []

    def abort(self) -> None:
        self.records.clear()
