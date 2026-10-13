"""Retry delays: full-jitter exponential backoff, and Retry-After parsing."""

from __future__ import annotations

import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def is_retryable_status(status: int) -> bool:
    return status in RETRY_STATUSES or (status >= 500 and status not in (501, 505))


def full_jitter(attempt: int, base: float, cap: float, rng: random.Random | None = None) -> float:
    """AWS-style full jitter: uniform(0, min(cap, base * 2**attempt))."""
    r = rng or random
    return r.uniform(0, min(cap, base * (2**attempt)))


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Seconds to wait from a Retry-After header (delta-seconds or HTTP-date); None if absent."""
    if not value:
        return None
    v = value.strip()
    if v.isdigit():
        return float(v)
    try:
        when = parsedate_to_datetime(v)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0.0, (when - now).total_seconds())
