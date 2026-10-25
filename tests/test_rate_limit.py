"""Per-device token bucket on POST /readings: burst 5, refill 1 per second."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.ratelimit import RateLimiter
from tests.test_metrics import sample
from tests.test_readings import reading


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_bucket_allows_burst_then_refills():
    clock = FakeClock()
    rl = RateLimiter(burst=5, per_second=1.0, clock=clock)
    assert [rl.acquire("dev-001") for _ in range(5)] == [0.0] * 5
    assert rl.acquire("dev-001") == pytest.approx(1.0)  # empty: next token in 1 s
    assert rl.acquire("dev-002") == 0.0  # other devices have their own bucket
    clock.now = 0.5
    assert rl.acquire("dev-001") == pytest.approx(0.5)
    clock.now = 1.0
    assert rl.acquire("dev-001") == 0.0  # one token refilled


@pytest.fixture
def limited_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "rl.db"))  # real defaults: burst 5, 1/s
    with TestClient(create_app()) as c:
        yield c


def test_sixth_reading_in_a_burst_gets_429_with_retry_after(limited_client):
    before = sample("sensor_rate_limited_total", route="/readings")
    codes = [limited_client.post("/readings", json=reading()).status_code for _ in range(6)]
    assert codes == [201] * 5 + [429]
    resp = limited_client.post("/readings", json=reading())
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "1"
    assert resp.json()["type"] == "urn:sensor-api:problem:rate-limited"
    # another device is unaffected
    assert limited_client.post("/readings", json=reading(device_id="dev-002")).status_code == 201
    assert sample("sensor_rate_limited_total", route="/readings") - before == 2
