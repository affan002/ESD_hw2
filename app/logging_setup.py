"""JSON logging: one object per line on stdout, with the current request ID attached."""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

from app.timeutil import utc_iso

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# Only these `extra=` keys are emitted. Anything else is dropped, so request
# bodies or other unexpected data can't leak into logs by accident.
EXTRA_FIELDS = (
    "device_id",
    "reading_id",
    "is_anomaly",
    "anomaly_reasons",
    "temperature_c",
    "humidity_pct",
    "battery_pct",
    "method",
    "path",
    "status_code",
    "duration_ms",
    "errors",
    "fault",
    "delay_ms",
    "config",
    "error",
    "stats",
    "transport",
    "retry_after_s",
    "existing_reading_id",
    "rpc_method",
    "grpc_code",
    "grpc_details",
)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": utc_iso(datetime.fromtimestamp(record.created, tz=timezone.utc)),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "event": getattr(record, "event", "log"),
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        for key in EXTRA_FIELDS:
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["stack"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(service: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # uvicorn installs its own handlers unless log_config=None; make sure its
    # loggers propagate to the root JSON handler either way.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    # httpx logs every request at INFO; the simulator does its own logging.
    logging.getLogger("httpx").setLevel(logging.WARNING)
