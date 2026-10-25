"""Prometheus metrics: every metric the API exposes is defined here.

Uses prometheus_client's default registry, which also provides process_* and
python_* metrics for free. The API runs a single uvicorn worker, so no
multiprocess mode is needed (see CLAUDE.md).

Naming: `sensor_` prefix, snake_case, base-unit suffix (_seconds, _total).
Labels only ever take values from small fixed sets; never device_id or request_id.
"""

import logging
import sqlite3

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from app import db
from app.anomaly import REASON_CODES, compute_status
from app.timeutil import parse_iso, utc_now

log = logging.getLogger("app.metrics")

# ---------- HTTP (recorded in app/middleware.py for every request) ----------

HTTP_REQUESTS = Counter(
    "sensor_http_requests_total",
    "HTTP requests handled, by route template and status code.",
    ["method", "route", "status_code"],
)

# Bucket bounds (seconds). histogram_quantile() interpolates linearly inside a
# bucket, so bounds are dense where normal POST /readings latency sits
# (10-40 ms) and sparse above, but still bracket the 500 ms injected delay.
# With only 10/25/50 ms bounds, p99 was estimated at 44 ms when the true value
# was 25 ms (see docs/BUILD_LOG.md, stage B7).
LATENCY_BUCKETS = (0.005, 0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05, 0.075, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
HTTP_DURATION = Histogram(
    "sensor_http_request_duration_seconds",
    "Time from receiving a request to finishing its response.",
    ["method", "route"],
    buckets=LATENCY_BUCKETS,
)

HTTP_IN_PROGRESS = Gauge(
    "sensor_http_requests_in_progress",
    "Requests currently being handled.",
)

# ---------- gRPC (recorded in app/grpc_server.py, ObservabilityInterceptor) ----------
# Measured on the server, like the HTTP metrics above, with the same buckets so the
# two transports can be compared directly. `method` is the RPC name and `code` the
# gRPC status name: both small fixed sets.

GRPC_REQUESTS = Counter(
    "sensor_grpc_requests_total",
    "gRPC calls handled, by RPC method and status code.",
    ["method", "code"],
)
GRPC_DURATION = Histogram(
    "sensor_grpc_request_duration_seconds",
    "Time from the server receiving a gRPC call to its handler returning.",
    ["method"],
    buckets=LATENCY_BUCKETS,  # same bounds as sensor_http_request_duration_seconds
)
GRPC_IN_PROGRESS = Gauge(
    "sensor_grpc_requests_in_progress",
    "gRPC calls currently being handled.",
    ["method"],
)
GRPC_METHODS = ("SubmitReading",)
GRPC_CODES = ("OK", "INVALID_ARGUMENT", "ALREADY_EXISTS", "RESOURCE_EXHAUSTED", "INTERNAL")

# ---------- Ingestion (recorded in app/ingest.py, shared by HTTP and gRPC) ----------

TRANSPORTS = ("http", "grpc")
READING_OUTCOMES = ("normal", "anomaly", "replayed", "conflict", "rejected", "failed")
READINGS = Counter(
    "sensor_readings_total",
    "Reading submissions by transport and outcome: normal/anomaly = stored (201/OK), "
    "replayed = identical repeat, nothing stored (200/OK), conflict = same device and timestamp "
    "with different values (409/ALREADY_EXISTS), rejected = invalid (422/INVALID_ARGUMENT), "
    "failed = server error (5xx/INTERNAL).",
    ["transport", "outcome"],
)

RATE_LIMITED = Counter(
    "sensor_rate_limited_total",
    "Requests refused by the per-device rate limiter (HTTP 429 / gRPC RESOURCE_EXHAUSTED), "
    "by HTTP route or RPC method.",
    ["route"],
)

VALIDATION_FIELDS = ("device_id", "timestamp", "temperature_c", "humidity_pct", "battery_pct")
VALIDATION_ERRORS = Counter(
    "sensor_reading_validation_errors_total",
    "Field-level validation failures on POST /readings. One rejected reading can fail several fields.",
    ["field", "reason"],
)

ANOMALIES = Counter(
    "sensor_anomalies_total",
    "Anomaly reasons found on stored readings. One reading can have several reasons.",
    ["reason"],
)

# A Histogram, not a Summary: two processes (API and gRPC server) write to the
# same database, and only histogram buckets can be summed across processes.
DB_WRITE = Histogram(
    "sensor_db_write_duration_seconds",
    "Time to store one reading and update its device row (one SQLite transaction).",
    buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.015, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)

# ---------- Fault injection (recorded in app/faults.py) ----------

FAULT_TYPES = ("delay", "error")
FAULTS_INJECTED = Counter(
    "sensor_faults_injected_total",
    "Faults injected into POST /readings requests.",
    ["fault"],
)
FAULT_ACTIVE = Gauge(
    "sensor_fault_active",
    "1 while this fault type is switched on via /admin/faults, else 0.",
    ["fault"],
)

# Create every known label combination up front so each series exists at 0
# from the first scrape; rate() and dashboards then work before the first event.
for _transport in TRANSPORTS:
    for _outcome in READING_OUTCOMES:
        READINGS.labels(_transport, _outcome)
RATE_LIMITED.labels("/readings")
RATE_LIMITED.labels("SubmitReading")
for _method in GRPC_METHODS:
    GRPC_IN_PROGRESS.labels(_method)
    GRPC_DURATION.labels(_method)
    for _code in GRPC_CODES:
        GRPC_REQUESTS.labels(_method, _code)
for _reason in REASON_CODES:
    ANOMALIES.labels(_reason)
for _fault in FAULT_TYPES:
    FAULTS_INJECTED.labels(_fault)
    FAULT_ACTIVE.labels(_fault).set(0)


def validation_field(loc: tuple) -> str:
    """Map a Pydantic error location to a bounded label value."""
    parts = [str(p) for p in loc if p != "body"]
    return parts[0] if parts and parts[0] in VALIDATION_FIELDS else "body"


# ---------- Device status (computed at scrape time) ----------


class DeviceStatusCollector(Collector):
    """Exposes sensor_devices{status}: how many devices are ok / anomaly / stale right now.

    Status is never stored; GET /devices computes it on read. This collector does
    the same on every scrape, with the same compute_status() rule, so the metric
    always agrees with the API and the dashboard.
    """

    def __init__(self) -> None:
        self._settings = None

    def configure(self, settings) -> None:
        self._settings = settings

    def collect(self):
        family = GaugeMetricFamily(
            "sensor_devices",
            "Devices by current status, computed at scrape time with the same rule as GET /devices.",
            labels=["status"],
        )
        if self._settings is not None:
            counts = {"ok": 0, "anomaly": 0, "stale": 0}
            try:
                conn = db.connect(self._settings.db_path)
                try:
                    now = utc_now()
                    for device in db.list_devices(conn):
                        status = compute_status(
                            parse_iso(device["last_seen"]),
                            now,
                            device["latest_reading"]["is_anomaly"],
                            self._settings.stale_after_s,
                        )
                        counts[status] += 1
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                # Skip the series rather than fail the whole scrape.
                log.warning("device status collection failed", extra={"event": "metrics_collect_failed", "error": str(exc)})
                counts = None
            if counts is not None:
                for status, value in counts.items():
                    family.add_metric([status], value)
        yield family


DEVICE_STATUS = DeviceStatusCollector()
REGISTRY.register(DEVICE_STATUS)


def render() -> tuple[bytes, str]:
    """Current metrics in the Prometheus text format, and its content type."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
