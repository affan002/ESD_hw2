import logging
import time

from tests.test_readings import reading, stored_count


def timed_post(client):
    start = time.perf_counter()
    resp = client.post("/readings", json=reading())
    return resp, time.perf_counter() - start


def test_faults_off_by_default(client):
    assert client.get("/admin/faults").json() == {"delay_ms": 0, "delay_every_n": 1, "error_every_n": 0}


def test_injected_error_returns_500_and_stores_nothing_then_delete_recovers(client, caplog):
    caplog.set_level(logging.INFO)
    assert client.put("/admin/faults", json={"error_every_n": 1}).status_code == 200
    resp = client.post("/readings", json=reading())
    assert resp.status_code == 500
    assert resp.json()["status"] == 500 and resp.json()["detail"] == "injected fault"
    assert stored_count(client) == 0
    assert any(getattr(r, "fault", None) == "error" for r in caplog.records)

    assert client.delete("/admin/faults").json()["error_every_n"] == 0
    assert client.post("/readings", json=reading()).status_code == 201
    assert stored_count(client) == 1


def test_injected_delay(client):
    client.put("/admin/faults", json={"delay_ms": 200, "delay_every_n": 1})
    resp, elapsed = timed_post(client)
    assert resp.status_code == 201
    assert elapsed >= 0.2


def test_delay_every_n_only_hits_every_nth_request(client):
    client.put("/admin/faults", json={"delay_ms": 200, "delay_every_n": 2})
    elapsed = [timed_post(client)[1] for _ in range(4)]
    slow = [e >= 0.2 for e in elapsed]
    assert slow == [False, True, False, True]


def test_error_every_n(client):
    client.put("/admin/faults", json={"error_every_n": 3})
    codes = [client.post("/readings", json=reading()).status_code for _ in range(6)]
    assert codes == [201, 201, 500, 201, 201, 500]


def test_invalid_fault_config_rejected(client):
    assert client.put("/admin/faults", json={"delay_ms": 6000}).status_code == 422
    assert client.put("/admin/faults", json={"delay_every_n": 0}).status_code == 422
    assert client.put("/admin/faults", json={"error_every_n": -1}).status_code == 422
