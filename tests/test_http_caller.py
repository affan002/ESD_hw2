"""The caller's retry and circuit-breaker logic, against a fake transport (no server)."""

import httpx
import pytest

from clients.http_caller import CircuitBreaker, CircuitOpenError, ReadingsCaller, RetriesExhausted, new_reading


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def make_caller(statuses, clock=None):
    """A caller whose server answers with `statuses` in order ('timeout' raises a timeout)."""
    sent = []

    def handler(request):
        sent.append(request.headers["x-request-id"])
        status = statuses.pop(0)
        if status == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(status, json={})

    caller = ReadingsCaller("http://api", CircuitBreaker(clock=clock or FakeClock()), sleep=lambda s: None)
    caller.http = httpx.Client(base_url="http://api", transport=httpx.MockTransport(handler))
    return caller, sent


def test_transient_500_is_retried_with_the_same_request_id():
    caller, sent = make_caller([500, 201])
    assert caller.submit(new_reading("dev-900")).status_code == 201
    assert len(sent) == 2 and sent[0] == sent[1]
    assert caller.breaker.state == "closed"


def test_4xx_is_not_retried():
    caller, sent = make_caller([422])
    assert caller.submit(new_reading("dev-900")).status_code == 422
    assert len(sent) == 1


def test_three_failures_open_the_breaker_then_calls_are_blocked():
    caller, sent = make_caller(["timeout", "timeout", 503])
    with pytest.raises(RetriesExhausted):
        caller.submit(new_reading("dev-900"))
    assert caller.breaker.state == "open"
    with pytest.raises(CircuitOpenError):
        caller.submit(new_reading("dev-900"))
    assert len(sent) == 3  # the blocked call sent nothing


def test_breaker_half_opens_after_recovery_and_closes_on_success():
    clock = FakeClock()
    caller, sent = make_caller([500, 500, 500, 201], clock)
    with pytest.raises(RetriesExhausted):
        caller.submit(new_reading("dev-900"))
    clock.now = 9.9
    with pytest.raises(CircuitOpenError):
        caller.submit(new_reading("dev-900"))
    clock.now = 10.0
    assert caller.submit(new_reading("dev-900")).status_code == 201
    assert caller.breaker.state == "closed"


def test_failed_half_open_trial_reopens():
    clock = FakeClock()
    breaker = CircuitBreaker(clock=clock)
    for _ in range(3):
        breaker.record_failure()
    clock.now = 10.0
    assert breaker.allow() and breaker.state == "half_open"
    breaker.record_failure()
    assert breaker.state == "open" and not breaker.allow()


def test_refused_call_is_not_reported_as_ok(caplog):
    import logging

    from clients.http_caller import call

    caplog.set_level(logging.INFO)
    caller, _ = make_caller([429])
    call(caller, "call 1", "dev-900")
    assert any("REFUSED: HTTP 429" in r.getMessage() for r in caplog.records)
    assert not any("-> OK" in r.getMessage() for r in caplog.records)
