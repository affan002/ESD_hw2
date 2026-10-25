"""In-memory token-bucket rate limiter (single instance).

Each key (here: a device_id) has a bucket holding up to `burst` tokens, refilled at
`per_second` tokens per second. A request takes one token; with none left it's refused,
and the caller is told how long until the next token arrives (the Retry-After value).

State lives in this process only: a restart resets it, and a second API instance
would keep its own buckets. Keys are bounded because device_id must match
^dev-\\d{3}$ (at most 1000 buckets).
"""

import logging
import math
import threading
import time

from app import metrics

log = logging.getLogger("app.ratelimit")


class RateLimiter:
    def __init__(self, burst: int, per_second: float, clock=time.monotonic) -> None:
        self.burst = burst
        self.per_second = per_second
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last refill time)

    def acquire(self, key: str) -> float:
        """Take a token for `key`. Returns 0.0 if allowed, else seconds until a token is available."""
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (float(self.burst), now))
            tokens = min(float(self.burst), tokens + (now - last) * self.per_second)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                return 0.0
            self._buckets[key] = (tokens, now)
            return (1.0 - tokens) / self.per_second

    def check(self, key: str, route: str) -> int | None:
        """Take a token for `key`. Returns None if allowed; otherwise counts and logs the refusal
        and returns the whole seconds to wait (the Retry-After value, at least 1)."""
        wait_s = self.acquire(key)
        if wait_s <= 0:
            return None
        retry_after = max(1, math.ceil(wait_s))
        metrics.RATE_LIMITED.labels(route).inc()
        log.warning("rate limited", extra={"event": "rate_limited", "device_id": key, "retry_after_s": retry_after})
        return retry_after

    def describe(self) -> str:
        return f"a burst of {self.burst} readings per device, refilled at {self.per_second:g} per second"
