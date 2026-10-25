"""POST /readings is idempotent on (device_id, timestamp): a retried request is safe."""

from tests.test_metrics import sample
from tests.test_readings import reading, stored_count


def test_valid_reading_returns_201_with_location(client):
    resp = client.post("/readings", json=reading())
    assert resp.status_code == 201
    assert resp.headers["location"] == f"/readings/{resp.json()['id']}"
    assert client.get(resp.headers["location"]).json() == resp.json()


def test_repeated_reading_is_replayed_not_stored_twice(client):
    body = reading()
    first = client.post("/readings", json=body)
    replayed = sample("sensor_readings_total", transport="http", outcome="replayed")
    second = client.post("/readings", json=body)
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.headers["idempotent-replayed"] == "true"
    assert second.json() == first.json()
    assert second.headers["location"] == first.headers["location"]
    assert stored_count(client) == 1
    assert client.get("/devices/dev-001").json()["reading_count"] == 1
    assert sample("sensor_readings_total", transport="http", outcome="replayed") - replayed == 1


def test_same_key_with_different_values_is_409(client):
    body = reading()
    first = client.post("/readings", json=body)
    resp = client.post("/readings", json={**body, "temperature_c": 30.0})
    assert resp.status_code == 409
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.json()["type"] == "urn:sensor-api:problem:reading-conflict"
    assert f"reading {first.json()['id']}" in resp.json()["detail"]
    assert stored_count(client) == 1


def test_get_unknown_reading_is_404_problem(client):
    resp = client.get("/readings/12345")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "reading not found"


def test_malformed_device_id_in_path_is_422_not_404(client):
    resp = client.get("/devices/not-a-device")
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["field"] == "path.device_id"


def test_wrong_method_is_405_problem_with_allow(client):
    resp = client.delete("/readings")
    assert resp.status_code == 405
    assert resp.json()["title"] == "Method Not Allowed"
    assert "POST" in resp.headers["allow"]


def test_replay_is_not_counted_as_a_db_write(client):
    body = reading()
    client.post("/readings", json=body)
    writes = sample("sensor_db_write_duration_seconds_count")
    assert client.post("/readings", json=body).status_code == 200
    assert sample("sensor_db_write_duration_seconds_count") == writes


def test_every_500_has_the_same_problem_type(client):
    client.put("/admin/faults", json={"error_every_n": 1})
    fault = client.post("/readings", json=reading()).json()
    assert fault["type"] == "urn:sensor-api:problem:internal-server-error"
