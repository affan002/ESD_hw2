import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    # Most tests post many readings for one device in milliseconds; the rate
    # limiter has its own tests (test_rate_limit.py) with the real defaults.
    monkeypatch.setenv("RATE_LIMIT_BURST", "1000")
    with TestClient(create_app()) as c:
        yield c
