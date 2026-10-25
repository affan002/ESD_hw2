import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.test_readings import reading


def post(client, **overrides):
    resp = client.post("/readings", json=reading(**overrides))
    assert resp.status_code == 201
    return resp.json()


def test_list_devices_sorted_with_count_and_status(client):
    post(client, device_id="dev-003")
    post(client, device_id="dev-001")
    post(client, device_id="dev-002", temperature_c=40.0)
    body = client.get("/devices").json()
    assert body["count"] == 3
    assert [d["device_id"] for d in body["devices"]] == ["dev-001", "dev-002", "dev-003"]
    statuses = {d["device_id"]: d["status"] for d in body["devices"]}
    assert statuses == {"dev-001": "ok", "dev-002": "anomaly", "dev-003": "ok"}


def test_list_devices_status_filter(client):
    post(client, device_id="dev-001")
    post(client, device_id="dev-002", battery_pct=5.0)
    body = client.get("/devices", params={"status": "anomaly"}).json()
    assert body["count"] == 1
    assert body["devices"][0]["device_id"] == "dev-002"
    assert client.get("/devices", params={"status": "broken"}).status_code == 422


def test_get_device_returns_latest_reading_and_counts(client):
    post(client, device_id="dev-001", temperature_c=40.0)
    latest = post(client, device_id="dev-001")
    body = client.get("/devices/dev-001").json()
    assert body["reading_count"] == 2
    assert body["anomaly_count"] == 1
    assert body["status"] == "ok"  # status follows the latest reading
    assert body["latest_reading"] == latest


def test_unknown_device_is_404(client):
    assert client.get("/devices/dev-999").status_code == 404
    problem = client.get("/devices/dev-999").json()
    assert (problem["status"], problem["title"], problem["detail"]) == (404, "Not Found", "device not found")
    assert client.get("/devices/dev-999/readings").status_code == 404


def test_history_newest_first_and_limit(client):
    ids = [post(client, device_id="dev-001", temperature_c=20.0 + i)["id"] for i in range(5)]
    body = client.get("/devices/dev-001/readings", params={"limit": 2}).json()
    assert body["device_id"] == "dev-001"
    assert [r["id"] for r in body["readings"]] == ids[::-1][:2]
    assert len(client.get("/devices/dev-001/readings").json()["readings"]) == 5


@pytest.mark.parametrize("limit", [0, 501, "x"])
def test_limit_bounds(client, limit):
    post(client, device_id="dev-001")
    assert client.get("/devices/dev-001/readings", params={"limit": limit}).status_code == 422
    assert client.get("/anomalies", params={"limit": limit}).status_code == 422


def test_anomalies_only_and_fleet_anomalies(client):
    post(client, device_id="dev-001")
    a1 = post(client, device_id="dev-001", humidity_pct=90.0)
    a2 = post(client, device_id="dev-002", temperature_c=5.0)
    body = client.get("/devices/dev-001/readings", params={"anomalies_only": "true"}).json()
    assert [r["id"] for r in body["readings"]] == [a1["id"]]
    fleet = client.get("/anomalies").json()["readings"]
    assert [r["id"] for r in fleet] == [a2["id"], a1["id"]]
    assert fleet[0]["anomaly_reasons"] == ["temperature_low"]


def test_device_goes_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "stale.db"))
    monkeypatch.setenv("STALE_AFTER_S", "-1")  # any device is immediately stale
    with TestClient(create_app()) as c:
        post(c, device_id="dev-001", temperature_c=40.0)
        assert c.get("/devices/dev-001").json()["status"] == "stale"
        assert c.get("/devices", params={"status": "stale"}).json()["count"] == 1
