"""The business rule for submitting a reading, shared by HTTP (app/main.py) and gRPC
(app/grpc_server.py), so both transports apply exactly the same checks, write to the
same database, and record the same logs and metrics.

Transport-specific concerns stay in the callers: request parsing, rate limiting,
fault injection, and mapping the exceptions below to an HTTP or gRPC status.
"""

import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import timedelta

from app import db, metrics
from app.anomaly import evaluate
from app.config import Settings
from app.models import ReadingIn, ReadingOut
from app.timeutil import utc_iso, utc_now

log = logging.getLogger("app")


class FutureTimestamp(Exception):
    """The device timestamp is too far ahead of server time (HTTP 422 / INVALID_ARGUMENT)."""

    type = "timestamp_in_future"

    def __init__(self, skew_s: float):
        super().__init__(f"timestamp is more than {skew_s:g}s in the future")


class ReadingConflict(Exception):
    """Same device and timestamp as a stored reading, but different values (HTTP 409 / ALREADY_EXISTS)."""

    def __init__(self, existing: ReadingOut):
        super().__init__(
            f"device {existing.device_id} already has a different reading at {existing.timestamp} "
            f"(reading {existing.id})"
        )
        self.existing = existing


@dataclass(frozen=True)
class SubmitResult:
    reading: ReadingOut
    created: bool  # False: an identical reading was already stored (idempotent replay)


def field_name(loc) -> str:
    """A Pydantic error location as a field name: ("body", "humidity_pct") -> "humidity_pct"."""
    return ".".join(str(p) for p in loc if p != "body") or "body"


def record_rejection(errors: list[dict], device_id: str | None, transport: str) -> None:
    """Count and log an invalid reading. `errors` are Pydantic-style dicts with `loc` and `type`."""
    metrics.READINGS.labels(transport, "rejected").inc()
    for e in errors:
        metrics.VALIDATION_ERRORS.labels(metrics.validation_field(e["loc"]), e["type"]).inc()
    # Log only the field and error type, never the submitted values.
    fields = [{"field": field_name(e["loc"]), "type": e["type"]} for e in errors]
    extra = {"event": "reading_rejected", "errors": fields, "transport": transport}
    if device_id is not None:
        extra["device_id"] = device_id
    log.warning("reading rejected", extra=extra)


def _same_values(a: ReadingOut, b: ReadingIn) -> bool:
    return (a.temperature_c, a.humidity_pct, a.battery_pct) == (b.temperature_c, b.humidity_pct, b.battery_pct)


def submit_reading(conn: sqlite3.Connection, reading: ReadingIn, settings: Settings, transport: str) -> SubmitResult:
    """Validate, classify and store one reading. Idempotent on (device_id, timestamp).

    Raises FutureTimestamp or ReadingConflict; database errors propagate.
    """
    received_at = utc_now()
    if reading.timestamp - received_at > timedelta(seconds=settings.future_skew_s):
        raise FutureTimestamp(settings.future_skew_s)

    reasons = evaluate(reading.temperature_c, reading.humidity_pct, reading.battery_pct)
    recorded_iso, received_iso = utc_iso(reading.timestamp), utc_iso(received_at)
    try:
        start = time.perf_counter()
        reading_id, created, is_new_device = db.insert_reading(
            conn,
            device_id=reading.device_id,
            recorded_at=recorded_iso,
            received_at=received_iso,
            temperature_c=reading.temperature_c,
            humidity_pct=reading.humidity_pct,
            battery_pct=reading.battery_pct,
            anomaly_reasons=reasons,
        )
    except Exception:
        metrics.READINGS.labels(transport, "failed").inc()
        raise
    if created:  # a replay only looked the reading up; don't count it as a write
        metrics.DB_WRITE.observe(time.perf_counter() - start)

    if not created:
        existing = ReadingOut(**db.get_reading(conn, reading_id))
        if not _same_values(existing, reading):
            metrics.READINGS.labels(transport, "conflict").inc()
            log.warning(
                "reading conflicts with a stored one",
                extra={"event": "reading_conflict", "device_id": reading.device_id,
                       "existing_reading_id": reading_id, "transport": transport},
            )
            raise ReadingConflict(existing)
        metrics.READINGS.labels(transport, "replayed").inc()
        log.info(
            "reading already stored, replaying",
            extra={"event": "reading_replayed", "device_id": reading.device_id,
                   "reading_id": reading_id, "transport": transport},
        )
        return SubmitResult(existing, created=False)

    metrics.READINGS.labels(transport, "anomaly" if reasons else "normal").inc()
    for reason in reasons:
        metrics.ANOMALIES.labels(reason).inc()

    if is_new_device:
        log.info("device registered", extra={"event": "device_registered", "device_id": reading.device_id})
    log.info(
        "reading stored",
        extra={
            "event": "reading_stored",
            "device_id": reading.device_id,
            "reading_id": reading_id,
            "is_anomaly": bool(reasons),
            "transport": transport,
        },
    )
    if reasons:
        log.warning(
            "anomaly detected: %s",
            ",".join(reasons),
            extra={
                "event": "anomaly_detected",
                "device_id": reading.device_id,
                "reading_id": reading_id,
                "anomaly_reasons": reasons,
                "temperature_c": reading.temperature_c,
                "humidity_pct": reading.humidity_pct,
                "battery_pct": reading.battery_pct,
            },
        )

    return SubmitResult(
        ReadingOut(
            id=reading_id,
            device_id=reading.device_id,
            timestamp=recorded_iso,
            received_at=received_iso,
            temperature_c=reading.temperature_c,
            humidity_pct=reading.humidity_pct,
            battery_pct=reading.battery_pct,
            is_anomaly=bool(reasons),
            anomaly_reasons=reasons,
        ),
        created=True,
    )
