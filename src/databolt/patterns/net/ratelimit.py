"""A minimal client-side rate limiter: at most N requests per second."""

from __future__ import annotations

import time
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self,
        requests_per_second: float | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval = 1.0 / requests_per_second if requests_per_second else 0.0
        self.clock = clock
        self.sleep = sleep
        self._next = 0.0

    def wait(self) -> None:
        if not self.interval:
            return
        now = self.clock()
        if now < self._next:
            self.sleep(self._next - now)
            now = self._next
        self._next = now + self.interval
