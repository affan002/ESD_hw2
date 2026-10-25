"""Metrics tests.

Metrics live in the process-wide default registry, so values accumulate across
tests. Always assert on the change (after - before), never on absolute values.
"""

from prometheus_client import REGISTRY


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_metrics_endpoint_serves_prometheus_text(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "# TYPE process_resident_memory_bytes gauge" in resp.text
    assert "python_info" in resp.text


def test_metrics_endpoint_not_in_openapi(client):
    assert "/metrics" not in client.get("/openapi.json").json()["paths"]


def test_http_counter_and_histogram_use_route_template(client):
    labels = {"method": "GET", "route": "/devices/{device_id}"}
    before_count = sample("sensor_http_requests_total", **labels, status_code="404")
    before_obs = sample("sensor_http_request_duration_seconds_count", **labels)
    client.get("/devices/dev-999")
    client.get("/devices/dev-998")
    assert sample("sensor_http_requests_total", **labels, status_code="404") - before_count == 2
    assert sample("sensor_http_request_duration_seconds_count", **labels) - before_obs == 2
    # the raw path must never become a label value
    assert REGISTRY.get_sample_value(
        "sensor_http_requests_total", {"method": "GET", "route": "/devices/dev-999", "status_code": "404"}
    ) is None


def test_unmatched_routes_share_one_label(client):
    before = sample("sensor_http_requests_total", method="GET", route="unmatched", status_code="404")
    client.get("/nope-1")
    client.get("/nope-2")
    assert sample("sensor_http_requests_total", method="GET", route="unmatched", status_code="404") - before == 2


def test_histogram_buckets_catch_slow_requests(client):
    labels = {"method": "POST", "route": "/readings"}
    fast_before = sample("sensor_http_request_duration_seconds_bucket", **labels, le="0.25")
    all_before = sample("sensor_http_request_duration_seconds_bucket", **labels, le="+Inf")
    client.put("/admin/faults", json={"delay_ms": 300, "delay_every_n": 1})
    from tests.test_readings import reading

    client.post("/readings", json=reading())
    assert sample("sensor_http_request_duration_seconds_bucket", **labels, le="0.25") - fast_before == 0
    assert sample("sensor_http_request_duration_seconds_bucket", **labels, le="+Inf") - all_before == 1


def test_in_progress_gauge_returns_to_baseline(client):
    before = sample("sensor_http_requests_in_progress")
    client.get("/health")
    assert sample("sensor_http_requests_in_progress") == before


# ---------- business metrics ----------

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402
from tests.test_readings import reading  # noqa: E402


def outcomes():
    return {o: sample("sensor_readings_total", transport="http", outcome=o) for o in ("normal", "anomaly", "rejected", "failed")}


def delta(before, after):
    return {k: after[k] - before[k] for k in before}


def test_all_outcome_and_reason_series_exist_at_zero(client):
    text = client.get("/metrics").text
    for transport in ("http", "grpc"):
        for outcome in ("normal", "anomaly", "replayed", "conflict", "rejected", "failed"):
            assert f'sensor_readings_total{{outcome="{outcome}",transport="{transport}"}}' in text
    for reason in ("temperature_low", "temperature_high", "humidity_low", "humidity_high", "battery_low"):
        assert f'sensor_anomalies_total{{reason="{reason}"}}' in text


def test_reading_outcomes_counted(client):
    before = outcomes()
    client.post("/readings", json=reading())
    client.post("/readings", json=reading(temperature_c=40.0))
    client.post("/readings", json=reading(humidity_pct=150))
    client.put("/admin/faults", json={"error_every_n": 1})
    client.post("/readings", json=reading())
    assert delta(before, outcomes()) == {"normal": 1, "anomaly": 1, "rejected": 1, "failed": 1}


def test_anomaly_reasons_counted_per_reason(client):
    hot = sample("sensor_anomalies_total", reason="temperature_high")
    wet = sample("sensor_anomalies_total", reason="humidity_high")
    low = sample("sensor_anomalies_total", reason="battery_low")
    client.post("/readings", json=reading(temperature_c=40.0, humidity_pct=90.0, battery_pct=5.0))
    assert sample("sensor_anomalies_total", reason="temperature_high") - hot == 1
    assert sample("sensor_anomalies_total", reason="humidity_high") - wet == 1
    assert sample("sensor_anomalies_total", reason="battery_low") - low == 1


def test_validation_errors_labelled_by_bounded_field_and_reason(client):
    humidity = sample("sensor_reading_validation_errors_total", field="humidity_pct", reason="less_than_equal")
    future = sample("sensor_reading_validation_errors_total", field="timestamp", reason="timestamp_in_future")
    body = sample("sensor_reading_validation_errors_total", field="body", reason="json_invalid")
    client.post("/readings", json=reading(humidity_pct=150))
    client.post("/readings", json=reading(timestamp="2099-01-01T00:00:00Z"))
    client.post("/readings", content=b"{not json", headers={"content-type": "application/json"})
    assert sample("sensor_reading_validation_errors_total", field="humidity_pct", reason="less_than_equal") - humidity == 1
    assert sample("sensor_reading_validation_errors_total", field="timestamp", reason="timestamp_in_future") - future == 1
    assert sample("sensor_reading_validation_errors_total", field="body", reason="json_invalid") - body == 1


def test_db_write_histogram_observes_stored_readings_only(client):
    count = sample("sensor_db_write_duration_seconds_count")
    total = sample("sensor_db_write_duration_seconds_sum")
    client.post("/readings", json=reading())
    client.post("/readings", json=reading(humidity_pct=150))  # rejected: never reaches the DB
    assert sample("sensor_db_write_duration_seconds_count") - count == 1
    assert sample("sensor_db_write_duration_seconds_sum") > total


def test_fault_gauges_and_counters(client):
    assert sample("sensor_fault_active", fault="delay") == 0
    injected = sample("sensor_faults_injected_total", fault="error")
    client.put("/admin/faults", json={"delay_ms": 1, "error_every_n": 2})
    assert sample("sensor_fault_active", fault="delay") == 1
    assert sample("sensor_fault_active", fault="error") == 1
    for _ in range(4):
        client.post("/readings", json=reading())
    assert sample("sensor_faults_injected_total", fault="error") - injected == 2
    client.delete("/admin/faults")
    assert sample("sensor_fault_active", fault="delay") == 0
    assert sample("sensor_fault_active", fault="error") == 0


def test_device_status_gauge_matches_api(client):
    client.post("/readings", json=reading(device_id="dev-001"))
    client.post("/readings", json=reading(device_id="dev-002", battery_pct=5.0))
    client.post("/readings", json=reading(device_id="dev-003"))
    client.get("/metrics")  # the gauge is computed during a scrape
    assert sample("sensor_devices", status="ok") == 2
    assert sample("sensor_devices", status="anomaly") == 1
    assert sample("sensor_devices", status="stale") == 0
    api = client.get("/devices").json()
    assert api["count"] == 3


def test_device_status_gauge_counts_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "stale.db"))
    monkeypatch.setenv("STALE_AFTER_S", "-1")
    with TestClient(create_app()) as c:
        c.post("/readings", json=reading(device_id="dev-001"))
        assert sample("sensor_devices", status="stale") == 1
        assert sample("sensor_devices", status="ok") == 0
