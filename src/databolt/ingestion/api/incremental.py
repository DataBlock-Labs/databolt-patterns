"""Incremental loading: send the last cursor, filter old records, propose the next cursor.

State is `{"cursor": <value>, "cursor_path": <path>}`, plus `"boundary_keys"` when a
`primary_key` is set. It is only a *proposal*: the caller commits it after landing
succeeds. The rules:

- **Start value.** The start is the committed cursor (minus `lookback`), else
  `initial_value`. Records whose cursor is below the start are dropped, in case the API
  ignored the filter.
- **Boundary.** The start is **inclusive** by default (`range_start: closed`). Records
  equal to the committed cursor can be new: a later event may share the last event's
  exact timestamp. Several settings decide what happens to them:
  - With a `primary_key`, the state remembers the keys of the records that sat exactly on
    the cursor (`boundary_keys`). A resumed run drops only those, so the boundary record
    is not landed twice and a new same-timestamp record is not lost.
  - Without a `primary_key`, the boundary record is landed again. That is safe, but it
    duplicates: dedupe downstream, or merge on a key.
  - `range_start: open` drops every record equal to the cursor. It never duplicates, but
    it can skip a record that shares the timestamp, so use it only for strictly
    increasing cursors (sequence ids).
- **Proposal.** The proposal is the maximum cursor among *landed* records, and it never
  moves backwards.
- **Truncated runs.** A truncated run advances only when `order: asc`. With any other
  order, the unfetched remainder may hold older records, and advancing would skip them
  forever.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from databolt.patterns.contract import Record, StateProposal
from databolt.patterns.errors import ValidationError
from databolt.patterns.selectors import get_path

# Cap on remembered boundary keys; beyond it the boundary is re-landed rather than skipped.
MAX_BOUNDARY_KEYS = 1000

Transform = Literal["none", "iso8601", "epoch_ms_to_iso", "epoch_s_to_iso"]


class IncrementalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cursor_path: str = Field(..., description="Dotted path to the cursor field in each record.")
    param: str | None = Field(
        None, description="Query param that receives the start value (e.g. starttime)."
    )
    initial_value: Any = Field(None, description="Start value for the first run.")
    transform: Transform = Field(
        "none",
        description="How to read the record's cursor value: as-is, an ISO-8601 string, or "
        "epoch milliseconds/seconds (sent to the API as ISO-8601).",
    )
    order: Literal["asc", "desc", "unordered"] = Field(
        "unordered",
        description="Order the API returns records in. Only 'asc' lets a truncated run "
        "advance the cursor.",
    )
    lookback: float = Field(
        0, ge=0, description="Re-read this many seconds (or units) before the cursor."
    )
    range_start: Literal["open", "closed"] = Field(
        "closed",
        description="'closed' (default) keeps records equal to the committed cursor (deduped "
        "by primary_key when set); 'open' drops them, which suits strictly increasing "
        "cursors only.",
    )
    primary_key: str | list[str] | None = Field(
        None,
        description="Dotted path(s) identifying a record, used to skip records already "
        "landed at the cursor boundary.",
    )


def _parse_iso(s: str) -> datetime:
    v = s.strip()
    if v.endswith("Z") or v.endswith("z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        raise ValidationError(f"not an ISO-8601 timestamp: {s!r}") from None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def format_iso(dt: datetime) -> str:
    """UTC, millisecond precision, `Z` suffix: 2026-10-12T08:30:00.123Z."""
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


class Incremental:
    """Tracks one run's cursor. Implements the runtime's StateTracker protocol."""

    def __init__(self, cfg: IncrementalConfig, state: dict[str, Any] | None) -> None:
        self.cfg = cfg
        self.warning: str | None = None
        stored = None
        if state and state.get("cursor") is not None:
            if state.get("cursor_path") in (None, cfg.cursor_path):
                stored = state["cursor"]
            else:
                self.warning = (
                    f"state cursor was for {state.get('cursor_path')!r}, config uses "
                    f"{cfg.cursor_path!r}; starting from initial_value"
                )
        self.resuming = stored is not None
        self.stored_boundary: set[str] = set()
        if stored is not None and state is not None:
            self.stored_boundary = {str(k) for k in state.get("boundary_keys") or []}
        self.max_boundary: set[str] = set()
        self.boundary_overflow = False
        self.stored_key = self._key(stored) if stored is not None else None
        start = stored if stored is not None else cfg.initial_value
        self.start_key = self._key(start) if start is not None else None
        if self.start_key is not None and cfg.lookback:
            self.start_key = self._minus(self.start_key, cfg.lookback)
        self.max_key: Any = None

    @property
    def is_time(self) -> bool:
        return self.cfg.transform != "none"

    def _key(self, v: Any) -> Any:
        """A comparable form of a cursor value (datetime, float or str)."""
        t = self.cfg.transform
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        if t in ("epoch_ms_to_iso", "epoch_s_to_iso"):
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                secs = v / 1000 if t == "epoch_ms_to_iso" else v
                return datetime.fromtimestamp(secs, tz=timezone.utc)
            if isinstance(v, str):
                return _parse_iso(v)
        if t == "iso8601":
            if isinstance(v, str):
                return _parse_iso(v)
            raise ValidationError(f"cursor value {v!r} is not an ISO-8601 string")
        if isinstance(v, bool):
            return str(v)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return _parse_iso(v)
            except ValidationError:
                return v
        return str(v)

    @staticmethod
    def _minus(key: Any, amount: float) -> Any:
        if isinstance(key, datetime):
            return key - timedelta(seconds=amount)
        if isinstance(key, float):
            return key - amount
        return key

    def _format(self, key: Any) -> Any:
        if isinstance(key, datetime):
            return format_iso(key)
        if isinstance(key, float) and key.is_integer():
            return int(key)
        return key

    @staticmethod
    def _lt(a: Any, b: Any) -> bool:
        try:
            return bool(a < b)
        except TypeError:
            return str(a) < str(b)

    def param_value(self) -> Any:
        """The value to send in `param`, or None on a first run with no initial_value."""
        return None if self.start_key is None else self._format(self.start_key)

    def record_key(self, record: Record) -> Any:
        v = get_path(record, self.cfg.cursor_path)
        if v is None:
            return None
        try:
            return self._key(v)
        except ValidationError:
            return None

    def pk(self, record: Record) -> str | None:
        pk = self.cfg.primary_key
        if pk is None:
            return None
        paths = [pk] if isinstance(pk, str) else pk
        values = [get_path(record, p) for p in paths]
        if all(v is None for v in values):
            return None
        return "|".join("" if v is None else str(v) for v in values)

    def keep(self, record: Record) -> bool:
        """False for records the cursor says were already loaded."""
        k = self.record_key(record)
        if k is None or self.start_key is None:
            return True
        if self._lt(k, self.start_key):
            return False
        if not self.resuming or self.stored_key is None:
            return True
        at_boundary = not self._lt(self.stored_key, k) and not self._lt(k, self.stored_key)
        if not at_boundary:
            return True
        if self.cfg.range_start == "open" and not self.cfg.lookback:
            return False
        key = self.pk(record)
        return key is None or key not in self.stored_boundary

    def observe(self, record: Record) -> None:
        k = self.record_key(record)
        if k is None:
            return
        if self.max_key is None or self._lt(self.max_key, k):
            self.max_key = k
            self.max_boundary = set()
            self.boundary_overflow = False
        if not self._lt(k, self.max_key):  # k == max
            key = self.pk(record)
            if key is not None:
                if len(self.max_boundary) < MAX_BOUNDARY_KEYS:
                    self.max_boundary.add(key)
                else:
                    self.boundary_overflow = True

    def proposal(self, truncated: bool) -> StateProposal:
        if self.max_key is None:
            return StateProposal(None, False, self.warning)
        if truncated and self.cfg.order != "asc":
            return StateProposal(
                None,
                False,
                f"run was truncated and records are not known to be ascending "
                f"(order={self.cfg.order}); the cursor was not advanced so that older "
                "records in the unfetched pages are not skipped",
            )
        boundary = self.max_boundary
        if self.stored_key is not None:
            if self._lt(self.max_key, self.stored_key):
                return StateProposal(None, False, self.warning)
            if not self._lt(self.stored_key, self.max_key):
                # Same cursor as before: only new boundary keys change the state.
                if boundary <= self.stored_boundary:
                    return StateProposal(None, False, self.warning)
                boundary = boundary | self.stored_boundary
        state: dict[str, Any] = {
            "cursor": self._format(self.max_key),
            "cursor_path": self.cfg.cursor_path,
        }
        if self.cfg.primary_key is not None and not self.boundary_overflow:
            state["boundary_keys"] = sorted(boundary)[:MAX_BOUNDARY_KEYS]
        return StateProposal(state, True, self.warning)
