import json
import logging
import re

from app.logging_setup import JsonFormatter, request_id_var

REQUIRED_KEYS = {"timestamp", "level", "service", "logger", "event", "message", "request_id"}


def _record(**extra):
    record = logging.LogRecord("app.test", logging.WARNING, __file__, 1, "hello %s", ("world",), None)
    for k, v in extra.items():
        setattr(record, k, v)
    return record


def test_formatter_emits_required_keys_as_json():
    line = JsonFormatter("sensor-api").format(_record(event="reading_stored", device_id="dev-001"))
    payload = json.loads(line)
    assert REQUIRED_KEYS <= payload.keys()
    assert payload["service"] == "sensor-api"
    assert payload["level"] == "WARNING"
    assert payload["message"] == "hello world"
    assert payload["event"] == "reading_stored"
    assert payload["device_id"] == "dev-001"
    assert payload["timestamp"].endswith("Z")


def test_formatter_drops_non_whitelisted_extras_and_uses_context_request_id():
    token = request_id_var.set("abc-123")
    try:
        payload = json.loads(JsonFormatter("sensor-api").format(_record(password="hunter2")))
    finally:
        request_id_var.reset(token)
    assert "password" not in payload
    assert payload["request_id"] == "abc-123"
    assert payload["event"] == "log"


def test_request_id_generated_when_missing(client):
    resp = client.get("/health")
    assert re.fullmatch(r"[0-9a-f]{32}", resp.headers["x-request-id"])


def test_request_id_echoed_when_valid(client):
    resp = client.get("/health", headers={"X-Request-ID": "test-123"})
    assert resp.headers["x-request-id"] == "test-123"


def test_request_id_replaced_when_invalid(client):
    resp = client.get("/health", headers={"X-Request-ID": "bad id with spaces!"})
    assert re.fullmatch(r"[0-9a-f]{32}", resp.headers["x-request-id"])


def test_access_log_uses_route_template(client, caplog):
    caplog.set_level(logging.INFO)
    client.get("/health", headers={"X-Request-ID": "test-456"})
    records = [r for r in caplog.records if getattr(r, "event", None) == "http_request"]
    assert records[-1].path == "/health"
    assert records[-1].status_code == 200
    client.get("/no-such-route")
    records = [r for r in caplog.records if getattr(r, "event", None) == "http_request"]
    assert records[-1].path == "unmatched"
    assert records[-1].status_code == 404


def test_unhandled_exception_returns_500_with_request_id(tmp_path, monkeypatch, caplog):
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    app = create_app()

    @app.get("/boom")
    def boom():
        raise RuntimeError("kaboom")

    caplog.set_level(logging.INFO)
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/boom", headers={"X-Request-ID": "crash-1"})
    assert resp.status_code == 500
    assert resp.headers["x-request-id"] == "crash-1"
    # same problem type as any other 500 (e.g. an injected fault)
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.json()["type"] == "urn:sensor-api:problem:internal-server-error"
    assert resp.json()["request_id"] == "crash-1"
    events = [getattr(r, "event", None) for r in caplog.records]
    assert "unhandled_exception" in events
