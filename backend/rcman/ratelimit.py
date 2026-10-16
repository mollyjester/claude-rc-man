"""A token bucket for each name: a burst, then a steady rate.

Used where a page could make rcman do work or write a line on every call it makes. Python
standard library only.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable

# The most names one set of buckets holds, so that names made up cannot grow it without end.
MOST_KEYS = 4096


class TokenBuckets:
    """``burst`` calls at once for each name, refilled at ``per_second``; ``clock`` is
    monotonic seconds. A name that has been quiet long enough to be full again is forgotten
    (it would start full), and when there are more than ``most_keys`` names the least recently
    used go."""

    def __init__(
        self,
        burst: int,
        per_second: float,
        clock: Callable[[], float] = time.monotonic,
        most_keys: int = MOST_KEYS,
    ):
        self.burst, self.per_second, self._clock, self._most = burst, per_second, clock, most_keys
        self._held: dict[str, tuple[float, float]] = {}  # name -> (tokens, when counted)
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._held)

    def _tokens(self, key: str, now: float) -> float:
        tokens, counted = self._held.get(key, (float(self.burst), now))
        return min(float(self.burst), tokens + (now - counted) * self.per_second)

    def take(self, key: str) -> bool:
        """Whether ``key`` may call now; a call that may is counted."""
        with self._lock:
            now = self._clock()
            self._forget(now)
            tokens = self._tokens(key, now)
            allowed = tokens >= 1.0
            self._held.pop(key, None)  # re-added last: the most recently used
            self._held[key] = (tokens - 1.0 if allowed else tokens, now)
            while len(self._held) > self._most:
                del self._held[next(iter(self._held))]
            return allowed

    def seconds_until_allowed(self, key: str) -> int:
        """Whole seconds until ``key`` may call, at least one."""
        with self._lock:
            missing = 1.0 - self._tokens(key, self._clock())
            return max(1, math.ceil(missing / self.per_second)) if missing > 0 else 1

    def _forget(self, now: float) -> None:
        for key in [k for k in self._held if self._tokens(k, now) >= self.burst]:
            del self._held[key]
