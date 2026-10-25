"""SQLite storage: schema, connections, and all queries.

Each request gets its own connection (see `get_conn` in main.py). Connections are
opened in autocommit mode; writes use an explicit BEGIN IMMEDIATE transaction.
"""

import json
import logging
import sqlite3
from pathlib import Path

log = logging.getLogger("app.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
  device_id       TEXT PRIMARY KEY,
  first_seen      TEXT NOT NULL,
  last_seen       TEXT NOT NULL,
  reading_count   INTEGER NOT NULL,
  anomaly_count   INTEGER NOT NULL,
  last_reading_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS readings (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id       TEXT NOT NULL,
  recorded_at     TEXT NOT NULL,
  received_at     TEXT NOT NULL,
  temperature_c   REAL NOT NULL,
  humidity_pct    REAL NOT NULL,
  battery_pct     REAL NOT NULL,
  is_anomaly      INTEGER NOT NULL,
  anomaly_reasons TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_readings_device_time ON readings(device_id, id DESC);
CREATE INDEX IF NOT EXISTS ix_readings_anomaly ON readings(is_anomaly, id DESC);
"""

# Idempotency key: a device can't take two readings at the same instant. insert_reading()
# enforces it (check-then-insert inside BEGIN IMMEDIATE); the unique index is a safety net
# and makes that check an index lookup. A database written before A2 may already hold
# duplicates (retries stored twice); then the index can't be unique, so a plain one is used.
UNIQUE_KEY_INDEX = "CREATE UNIQUE INDEX IF NOT EXISTS ux_readings_device_recorded ON readings(device_id, recorded_at)"
PLAIN_KEY_INDEX = "CREATE INDEX IF NOT EXISTS ix_readings_device_recorded ON readings(device_id, recorded_at)"

_READING_COLUMNS = (
    "r.id, r.device_id, r.recorded_at, r.received_at, r.temperature_c, "
    "r.humidity_pct, r.battery_pct, r.is_anomaly, r.anomaly_reasons"
)


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI may open, use and close a request's
    # connection on different threadpool threads (never concurrently).
    conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_schema(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        try:
            conn.execute(UNIQUE_KEY_INDEX)
        except sqlite3.IntegrityError:
            log.warning(
                "existing duplicate readings; idempotency key index created as non-unique",
                extra={"event": "schema_duplicate_readings"},
            )
            conn.execute(PLAIN_KEY_INDEX)
    finally:
        conn.close()


def ping(conn: sqlite3.Connection) -> None:
    conn.execute("SELECT 1").fetchone()


def insert_reading(
    conn: sqlite3.Connection,
    *,
    device_id: str,
    recorded_at: str,
    received_at: str,
    temperature_c: float,
    humidity_pct: float,
    battery_pct: float,
    anomaly_reasons: list[str],
) -> tuple[int, bool, bool]:
    """Store a reading and update its device in one transaction.

    Idempotent on (device_id, recorded_at): if that reading is already stored,
    nothing is written and the existing id is returned with created=False.
    Returns (reading_id, created, is_new_device).
    """
    is_anomaly = 1 if anomaly_reasons else 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT id FROM readings WHERE device_id = ? AND recorded_at = ?", (device_id, recorded_at)
        ).fetchone()
        if existing is not None:
            conn.execute("COMMIT")
            return existing["id"], False, False
        reading_id = conn.execute(
            "INSERT INTO readings (device_id, recorded_at, received_at, temperature_c, humidity_pct,"
            " battery_pct, is_anomaly, anomaly_reasons) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (device_id, recorded_at, received_at, temperature_c, humidity_pct, battery_pct,
             is_anomaly, json.dumps(anomaly_reasons)),
        ).lastrowid
        is_new_device = conn.execute(
            "SELECT 1 FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone() is None
        conn.execute(
            "INSERT INTO devices (device_id, first_seen, last_seen, reading_count, anomaly_count, last_reading_id)"
            " VALUES (?, ?, ?, 1, ?, ?)"
            " ON CONFLICT(device_id) DO UPDATE SET"
            "   last_seen = excluded.last_seen,"
            "   reading_count = reading_count + 1,"
            "   anomaly_count = anomaly_count + excluded.anomaly_count,"
            "   last_reading_id = excluded.last_reading_id",
            (device_id, received_at, received_at, is_anomaly, reading_id),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return reading_id, True, is_new_device


def _reading(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "device_id": row["device_id"],
        "timestamp": row["recorded_at"],
        "received_at": row["received_at"],
        "temperature_c": row["temperature_c"],
        "humidity_pct": row["humidity_pct"],
        "battery_pct": row["battery_pct"],
        "is_anomaly": bool(row["is_anomaly"]),
        "anomaly_reasons": json.loads(row["anomaly_reasons"]),
    }


def _device(row: sqlite3.Row) -> dict:
    return {
        "device_id": row["d_device_id"],
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "reading_count": row["reading_count"],
        "anomaly_count": row["anomaly_count"],
        "latest_reading": _reading(row),
    }


_DEVICE_QUERY = (
    "SELECT d.device_id AS d_device_id, d.first_seen, d.last_seen, d.reading_count, d.anomaly_count, "
    f"{_READING_COLUMNS} FROM devices d JOIN readings r ON r.id = d.last_reading_id"
)


def get_device(conn: sqlite3.Connection, device_id: str) -> dict | None:
    row = conn.execute(f"{_DEVICE_QUERY} WHERE d.device_id = ?", (device_id,)).fetchone()
    return _device(row) if row else None


def get_reading(conn: sqlite3.Connection, reading_id: int) -> dict | None:
    row = conn.execute(f"SELECT {_READING_COLUMNS} FROM readings r WHERE r.id = ?", (reading_id,)).fetchone()
    return _reading(row) if row else None


def list_devices(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(f"{_DEVICE_QUERY} ORDER BY d.device_id").fetchall()
    return [_device(r) for r in rows]


def list_readings(
    conn: sqlite3.Connection, device_id: str, limit: int, anomalies_only: bool = False
) -> list[dict]:
    sql = f"SELECT {_READING_COLUMNS} FROM readings r WHERE r.device_id = ?"
    if anomalies_only:
        sql += " AND r.is_anomaly = 1"
    rows = conn.execute(sql + " ORDER BY r.id DESC LIMIT ?", (device_id, limit)).fetchall()
    return [_reading(r) for r in rows]


def list_anomalies(conn: sqlite3.Connection, limit: int) -> list[dict]:
    rows = conn.execute(
        f"SELECT {_READING_COLUMNS} FROM readings r WHERE r.is_anomaly = 1 ORDER BY r.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_reading(r) for r in rows]


def count_readings(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
