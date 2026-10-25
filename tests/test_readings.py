import itertools
import logging
from datetime import datetime, timedelta, timezone

import pytest

from app import db


_ms = itertools.count()


def reading(**overrides):
    """A valid reading. Each call gets a distinct timestamp (1 ms apart), so two calls
    are two different readings, not an idempotent repeat."""
    body = {
        "device_id": "dev-001",
        "timestamp": (datetime.now(timezone.utc) - timedelta(seconds=10, milliseconds=next(_ms))).isoformat(),
        "temperature_c": 22.5,
        "humidity_pct": 45.0,
        "battery_pct": 88.0,
    }
    body.update(overrides)
    return body


def stored_count(client) -> int:
    conn = db.connect(client.app.state.settings.db_path)
    try:
        return db.count_readings(conn)
    finally:
        conn.close()


def test_valid_reading_is_stored(client):
    resp = client.post("/readings", json=reading())
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] >= 1
    assert body["device_id"] == "dev-001"
    assert body["is_anomaly"] is False
    assert body["anomaly_reasons"] == []
    assert body["timestamp"].endswith("Z") and body["received_at"].endswith("Z")
    assert stored_count(client) == 1


def test_hot_reading_is_stored_as_anomaly(client, caplog):
    caplog.set_level(logging.INFO)
    resp = client.post("/readings", json=reading(temperature_c=40.0))
    assert resp.status_code == 201
    assert resp.json()["is_anomaly"] is True
    assert resp.json()["anomaly_reasons"] == ["temperature_high"]
    events = [getattr(r, "event", None) for r in caplog.records]
    assert {"device_registered", "reading_stored", "anomaly_detected"} <= set(events)


def test_timestamp_normalised_to_utc(client):
    resp = client.post("/readings", json=reading(timestamp="2026-01-15T15:00:00+05:00"))
    assert resp.status_code == 201
    assert resp.json()["timestamp"] == "2026-01-15T10:00:00.000Z"


@pytest.mark.parametrize(
    "overrides,missing",
    [
        ({"humidity_pct": 150}, None),
        ({"temperature_c": -41}, None),
        ({"battery_pct": -1}, None),
        ({}, "temperature_c"),
        ({"device_id": "sensor-1"}, None),
        ({"timestamp": "2026-09-26T10:00:00"}, None),  # no timezone
        ({"timestamp": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}, None),
        ({"temperature_c": "hot"}, None),
    ],
)
def test_invalid_readings_are_rejected_and_not_stored(client, overrides, missing):
    body = reading(**overrides)
    if missing:
        body.pop(missing)
    resp = client.post("/readings", json=body)
    assert resp.status_code == 422
    assert resp.headers["content-type"] == "application/problem+json"
    problem = resp.json()
    assert problem["status"] == 422 and problem["type"] == "urn:sensor-api:problem:validation-error"
    assert problem["errors"] and problem["request_id"] == resp.headers["x-request-id"]
    assert stored_count(client) == 0


def test_future_timestamp_error_type(client):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    resp = client.post("/readings", json=reading(timestamp=future))
    assert resp.json()["errors"][0] == {
        "field": "timestamp",
        "type": "timestamp_in_future",
        "message": "timestamp is more than 60s in the future",
    }


def test_rejection_is_logged_without_values(client, caplog):
    caplog.set_level(logging.INFO)
    client.post("/readings", json=reading(humidity_pct=150))
    rec = next(r for r in caplog.records if getattr(r, "event", None) == "reading_rejected")
    assert rec.device_id == "dev-001"
    assert rec.errors == [{"field": "humidity_pct", "type": "less_than_equal"}]
    assert not hasattr(rec, "humidity_pct")
