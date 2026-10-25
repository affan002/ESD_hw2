"""A service-to-service caller for POST /readings, with a timeout, a limited retry with
backoff, and a circuit breaker. It acts like another service calling the API.

    .venv/bin/python -m clients.http_caller --calls 5     # several normal calls
    .venv/bin/python -m clients.http_caller --demo        # scripted failure demo (uses /admin/faults)

Settings (docs/DECISIONS.md, A-5):
- timeout 2 s per attempt (normal p99 is about 25 ms);
- at most 3 attempts per call, backoff 0.2 s then 0.4 s (doubling) plus up to 0.1 s jitter;
- retried only on a timeout, a connection error or a 5xx. A 4xx is never retried: repeating
  an invalid or rate-limited request can't fix it. Retrying a POST is safe only because the
  API is idempotent on (device_id, timestamp): a retry of a reading that was in fact stored
  gets 200 (replay), not a duplicate;
- the breaker opens after 3 consecutive failed attempts, rejects calls without sending
  anything while open, and after 10 s lets one trial call through (half-open): success
  closes it, failure opens it again.

Every attempt of one call sends the same X-Request-ID (cli-<12 hex>), so all attempts can
be found together in the API's logs.
"""

import argparse
import logging
import random
import sys
import time
import uuid
from datetime import datetime, timezone

import httpx

log = logging.getLogger("caller")

TIMEOUT_S = 2.0
MAX_ATTEMPTS = 3
BACKOFF_BASE_S = 0.2
JITTER_S = 0.1
FAILURE_THRESHOLD = 3
RECOVERY_S = 10.0


class CircuitOpenError(Exception):
    """The breaker is open: the call was not sent."""


class RetriesExhausted(Exception):
    """Every attempt failed with a timeout, connection error or 5xx."""


class CircuitBreaker:
    """closed → (N consecutive failures) → open → (recovery period) → half_open → closed or open."""

    def __init__(self, failure_threshold: int = FAILURE_THRESHOLD, recovery_s: float = RECOVERY_S, clock=time.monotonic):
        self.failure_threshold = failure_threshold
        self.recovery_s = recovery_s
        self._clock = clock
        self.state = "closed"
        self.failures = 0
        self.opened_at = 0.0

    def _set(self, state: str) -> None:
        if state != self.state:
            log.info("  breaker: %s -> %s", self.state, state)
            self.state = state

    def allow(self) -> bool:
        if self.state == "open" and self._clock() - self.opened_at >= self.recovery_s:
            self._set("half_open")  # let one trial call through
        return self.state != "open"

    def seconds_until_trial(self) -> float:
        return max(0.0, self.recovery_s - (self._clock() - self.opened_at)) if self.state == "open" else 0.0

    def record_success(self) -> None:
        self.failures = 0
        self._set("closed")

    def record_failure(self) -> None:
        self.failures += 1
        if self.state == "half_open" or self.failures >= self.failure_threshold:
            self.opened_at = self._clock()
            self._set("open")


class ReadingsCaller:
    def __init__(self, base_url: str, breaker: CircuitBreaker | None = None, sleep=time.sleep):
        self.http = httpx.Client(base_url=base_url, timeout=TIMEOUT_S)
        self.breaker = breaker or CircuitBreaker()
        self._sleep = sleep

    def submit(self, reading: dict) -> httpx.Response:
        """POST one reading. Returns the response (2xx or 4xx), or raises CircuitOpenError / RetriesExhausted."""
        request_id = f"cli-{uuid.uuid4().hex[:12]}"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if not self.breaker.allow():
                raise CircuitOpenError(f"circuit open, not sent; trial allowed in {self.breaker.seconds_until_trial():.1f} s")
            start = time.perf_counter()
            try:
                resp = self.http.post("/readings", json=reading, headers={"X-Request-ID": request_id})
            except httpx.TimeoutException:
                problem = f"timeout after {TIMEOUT_S:g} s"
            except httpx.TransportError as exc:
                problem = f"connection error ({type(exc).__name__})"
            else:
                if resp.status_code < 500:
                    # 2xx, or a 4xx that retrying can't fix. Either way the server answered: it's healthy.
                    self.breaker.record_success()
                    log.info("  attempt %d [%s]: HTTP %d in %.0f ms", attempt, request_id, resp.status_code,
                             (time.perf_counter() - start) * 1000)
                    return resp
                problem = f"HTTP {resp.status_code}"
            log.info("  attempt %d [%s]: %s", attempt, request_id, problem)
            self.breaker.record_failure()
            if attempt == MAX_ATTEMPTS:
                raise RetriesExhausted(f"{MAX_ATTEMPTS} attempts failed, last: {problem}")
            if self.breaker.state == "open":
                raise CircuitOpenError("circuit opened during this call; not retrying")
            backoff = BACKOFF_BASE_S * 2 ** (attempt - 1) + random.uniform(0, JITTER_S)
            log.info("  retrying in %.2f s", backoff)
            self._sleep(backoff)
        raise AssertionError("unreachable")


def new_reading(device_id: str) -> dict:
    return {
        "device_id": device_id,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "temperature_c": 22.0,
        "humidity_pct": 45.0,
        "battery_pct": 88.0,
    }


def call(caller: ReadingsCaller, label: str, device_id: str) -> None:
    log.info("%s", label)
    try:
        resp = caller.submit(new_reading(device_id))
    except (CircuitOpenError, RetriesExhausted) as exc:
        log.info("  -> FAILED: %s", exc)
        return
    if resp.is_success:
        extra = " (replayed)" if resp.headers.get("idempotent-replayed") else ""
        log.info("  -> OK: HTTP %d%s, %s", resp.status_code, extra, resp.headers.get("location", ""))
    else:  # 4xx: the server is healthy but refused this request; nothing was stored, and it isn't retried
        problem = resp.json() if resp.headers.get("content-type") == "application/problem+json" else {}
        log.info("  -> REFUSED: HTTP %d %s", resp.status_code, problem.get("detail", resp.text[:80]))


def demo(base_url: str, device_id: str) -> None:
    caller = ReadingsCaller(base_url)
    admin = httpx.Client(base_url=base_url, timeout=5.0)
    log.info("settings: timeout %g s · %d attempts · backoff %g s doubling + <=%g s jitter · "
             "breaker opens after %d failures, trial after %g s",
             TIMEOUT_S, MAX_ATTEMPTS, BACKOFF_BASE_S, JITTER_S, FAILURE_THRESHOLD, RECOVERY_S)
    admin.delete("/admin/faults")
    before = admin.get(f"/devices/{device_id}")
    count_before = before.json()["reading_count"] if before.status_code == 200 else 0

    # Calls are spaced about 1 s apart so the demo stays under the API's per-device
    # rate limit (burst 5, 1/s); rate limiting is demonstrated separately.
    log.info("== 1. healthy API")
    for i in range(1, 3):
        call(caller, f"call {i}", device_id)
        time.sleep(1)

    log.info("== 2. transient failure: every 2nd request returns 500 (PUT /admin/faults error_every_n=2)")
    admin.put("/admin/faults", json={"error_every_n": 2})
    call(caller, "call 3", device_id)
    time.sleep(1)
    call(caller, "call 4", device_id)
    time.sleep(1)

    log.info("== 3. API hangs: every request delayed 3 s, longer than the 2 s timeout")
    admin.put("/admin/faults", json={"delay_ms": 3000, "delay_every_n": 1})
    call(caller, "call 5", device_id)
    for i in range(6, 9):
        time.sleep(1)
        call(caller, f"call {i}", device_id)

    log.info("== 4. API recovers (DELETE /admin/faults); wait for the breaker's recovery period")
    admin.delete("/admin/faults")
    time.sleep(caller.breaker.seconds_until_trial() + 0.1)
    call(caller, "call 9 (half-open trial)", device_id)
    call(caller, "call 10", device_id)

    added = admin.get(f"/devices/{device_id}").json()["reading_count"] - count_before
    log.info("device %s gained %d readings in this run: calls 1-4, 9, 10, plus call 5's first attempt, which "
             "the server stored after the client had timed out (its retries were replays, not duplicates)",
             device_id, added)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--device", default="dev-900")
    p.add_argument("--calls", type=int, default=5, help="number of calls (ignored with --demo)")
    p.add_argument("--demo", action="store_true", help="run the scripted retry/breaker demo (stop the simulator first)")
    args = p.parse_args(argv)
    # A human-readable CLI: plain lines, not the services' JSON log format.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s.%(msecs)03d %(message)s", datefmt="%H:%M:%S", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.demo:
        demo(args.url, args.device)
    else:
        caller = ReadingsCaller(args.url)
        for i in range(1, args.calls + 1):
            call(caller, f"call {i}", args.device)
    return 0


if __name__ == "__main__":
    sys.exit(main())
