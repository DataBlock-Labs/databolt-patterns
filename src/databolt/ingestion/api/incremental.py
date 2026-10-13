"""Incremental loading: send the last cursor, filter old records, propose the next cursor.

State is `{"cursor": <value>, "cursor_path": <path>}`. It is only a *proposal*: the
caller commits it after landing succeeds. Rules:

- The start value is the committed cursor (minus `lookback`), else `initial_value`.
- Records whose cursor is below the start value are dropped (the API ignored the
  filter). With `range_start: open` (default), a resumed run also drops records equal
  to the committed cursor, so an inclusive API filter does not re-land the boundary row.
- The proposal is the maximum cursor among *landed* records, never moving backwards.
- A **truncated** run advances only when `order: asc`: with any other order the
  unfetched remainder may hold older records, and advancing would skip them forever.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from databolt.patterns.contract import Record, StateProposal
from databolt.patterns.errors import ValidationError
from databolt.patterns.selectors import get_path

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
        "open",
        description="'open' drops records equal to the committed cursor on a resumed run.",
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

    def keep(self, record: Record) -> bool:
        """False for records the cursor says were already loaded."""
        k = self.record_key(record)
        if k is None or self.start_key is None:
            return True
        if self._lt(k, self.start_key):
            return False
        open_range = self.cfg.range_start == "open" and self.resuming and not self.cfg.lookback
        return not (open_range and not self._lt(self.start_key, k))

    def observe(self, record: Record) -> None:
        k = self.record_key(record)
        if k is not None and (self.max_key is None or self._lt(self.max_key, k)):
            self.max_key = k

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
        if self.stored_key is not None and not self._lt(self.stored_key, self.max_key):
            return StateProposal(None, False, self.warning)
        state = {"cursor": self._format(self.max_key), "cursor_path": self.cfg.cursor_path}
        return StateProposal(state, True, self.warning)
