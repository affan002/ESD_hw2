"""FastAPI application factory and routes for the sensor ingestion service."""

import logging
import re
import sqlite3
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Annotated

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import db, ingest, metrics
from app.anomaly import compute_status
from app.config import Settings, load_settings
from app.faults import FaultConfig, FaultState
from app.middleware import RequestContextMiddleware
from app.models import (
    DEVICE_ID_PATTERN,
    AnomalyList,
    DeviceList,
    DeviceReadings,
    DeviceStatus,
    DeviceStatusValue,
    Problem,
    ReadingIn,
    ReadingOut,
)
from app.problems import MEDIA_TYPE as PROBLEM_MEDIA_TYPE
from app.problems import ProblemError, problem_response, slug_for
from app.ratelimit import RateLimiter
from app.timeutil import parse_iso, utc_now

log = logging.getLogger("app")

_DEVICE_ID_RE = re.compile(DEVICE_ID_PATTERN)
STATIC_DIR = Path(__file__).parent / "static"


def get_conn(request: Request):
    conn = db.connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
Limit = Annotated[int, Query(ge=1, le=500)]
DeviceId = Annotated[str, PathParam(pattern=DEVICE_ID_PATTERN, examples=["dev-001"])]

# OpenAPI declarations for error responses; every error body is a Problem.
NOT_FOUND = {404: {"model": Problem, "description": "No such resource"}}


def _validation_errors(exc: RequestValidationError) -> list[dict]:
    return [
        {
            "field": ingest.field_name(e["loc"]),
            "type": e["type"],
            "message": e.get("msg", ""),
        }
        for e in exc.errors()
    ]


def _problem_openapi(app: FastAPI) -> dict:
    """FastAPI's schema, with every error response declared as application/problem+json.

    FastAPI documents errors as application/json and auto-adds a 422 using its own
    HTTPValidationError model; both are rewritten to the Problem schema we really return.
    """
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
    problem_ref = {"$ref": "#/components/schemas/Problem"}
    for path_item in schema["paths"].values():
        for op in path_item.values():
            for resp in op.get("responses", {}).values():
                content = resp.get("content", {})
                ref = content.get("application/json", {}).get("schema", {}).get("$ref", "")
                if ref.endswith(("/Problem", "/HTTPValidationError")):
                    content.pop("application/json")
                    content[PROBLEM_MEDIA_TYPE] = {"schema": problem_ref}
    for name in ("HTTPValidationError", "ValidationError"):
        schema["components"]["schemas"].pop(name, None)
    app.openapi_schema = schema
    return schema


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init_schema(settings.db_path)
        log.info(
            "sensor-api starting",
            extra={"event": "startup", "config": {"db_path": settings.db_path, "stale_after_s": settings.stale_after_s}},
        )
        yield

    app = FastAPI(
        title="Sensor Ingestion API",
        version="0.2.0",
        description="Ingests IoT sensor readings and reports device status. "
        "Every error is an RFC 9457 problem (application/problem+json).",
        lifespan=lifespan,
    )
    app.openapi = lambda: _problem_openapi(app)
    app.state.settings = settings
    app.state.faults = faults = FaultState()
    limiter = RateLimiter(settings.rate_limit_burst, settings.rate_limit_per_s)
    metrics.DEVICE_STATUS.configure(settings)
    app.add_middleware(RequestContextMiddleware)

    # Operator dashboard: static files that call the JSON API below.
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(STATIC_DIR / "index.html")

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError):
        if request.method == "POST" and request.url.path == "/readings":
            body = exc.body
            device_id = None
            if isinstance(body, dict) and isinstance(body.get("device_id"), str) and _DEVICE_ID_RE.match(body["device_id"]):
                device_id = body["device_id"]
            ingest.record_rejection(list(exc.errors()), device_id, "http")
        else:
            errors = [{"field": e["field"], "type": e["type"]} for e in _validation_errors(exc)]
            log.warning("request rejected", extra={"event": "request_invalid", "errors": errors})
        return problem_response(
            422, "validation-error", "the request has invalid fields; see errors", request.url.path,
            errors=_validation_errors(exc),
        )

    @app.exception_handler(StarletteHTTPException)
    async def on_http_error(request: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else HTTPStatus(exc.status_code).phrase
        return problem_response(
            exc.status_code, slug_for(exc.status_code), detail, request.url.path, headers=exc.headers
        )

    @app.exception_handler(ProblemError)
    async def on_problem(request: Request, exc: ProblemError):
        return problem_response(exc.status, exc.slug, exc.detail, request.url.path, headers=exc.headers)

    @app.get("/metrics", include_in_schema=False)
    def metrics_endpoint():
        body, content_type = metrics.render()
        return Response(content=body, media_type=content_type)

    @app.get("/health")
    def health():
        try:
            conn = db.connect(settings.db_path)
            try:
                db.ping(conn)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            log.error("database health check failed", extra={"event": "health_check_failed", "error": str(exc)})
            return JSONResponse(status_code=503, content={"status": "degraded", "db": "error"})
        return {"status": "ok", "db": "ok"}

    @app.post(
        "/readings",
        status_code=201,
        response_model=ReadingOut,
        summary="Submit a reading",
        responses={
            201: {
                "description": "Stored. `Location` points to the new reading.",
                "headers": {"Location": {"description": "/readings/{id}", "schema": {"type": "string"}}},
            },
            200: {
                "model": ReadingOut,
                "description": "Idempotent replay: an identical reading (same device_id, timestamp and values) "
                "was already stored. Nothing new is written; the stored reading is returned.",
                "headers": {"Idempotent-Replayed": {"schema": {"type": "string", "enum": ["true"]}}},
            },
            409: {"model": Problem, "description": "Same device_id and timestamp as a stored reading, but different values"},
            429: {
                "model": Problem,
                "description": f"Rate limited: {limiter.describe()}",
                "headers": {"Retry-After": {"description": "Seconds until a request is allowed again", "schema": {"type": "integer"}}},
            },
            500: {"model": Problem, "description": "Server error; nothing stored. Safe to retry (idempotent)"},
        },
    )
    def create_reading(reading: ReadingIn, conn: Conn, response: Response):
        retry_after = limiter.check(reading.device_id, "/readings")
        if retry_after is not None:
            raise ProblemError(
                429,
                "rate-limited",
                f"device {reading.device_id} is over its limit ({limiter.describe()}); retry in {retry_after} s",
                headers={"Retry-After": str(retry_after)},
            )
        try:
            faults.apply()
        except HTTPException:
            metrics.READINGS.labels("http", "failed").inc()
            raise
        try:
            result = ingest.submit_reading(conn, reading, settings, transport="http")
        except ingest.FutureTimestamp as exc:
            raise RequestValidationError(
                [{"type": exc.type, "loc": ("body", "timestamp"), "msg": str(exc), "input": reading.timestamp.isoformat()}],
                body=reading.model_dump(mode="json"),
            )
        except ingest.ReadingConflict as exc:
            raise ProblemError(409, "reading-conflict", str(exc))
        response.headers["Location"] = f"/readings/{result.reading.id}"
        if not result.created:
            response.status_code = 200
            response.headers["Idempotent-Replayed"] = "true"
        return result.reading

    @app.get("/readings/{reading_id}", response_model=ReadingOut, responses=NOT_FOUND)
    def get_reading(reading_id: Annotated[int, PathParam(ge=1)], conn: Conn):
        reading = db.get_reading(conn, reading_id)
        if reading is None:
            raise HTTPException(status_code=404, detail="reading not found")
        return reading

    def with_status(device: dict) -> DeviceStatus:
        status = compute_status(
            parse_iso(device["last_seen"]),
            utc_now(),
            device["latest_reading"]["is_anomaly"],
            settings.stale_after_s,
        )
        return DeviceStatus(status=status, **device)

    @app.get("/devices", response_model=DeviceList)
    def list_devices(conn: Conn, status: DeviceStatusValue | None = None):
        devices = [with_status(d) for d in db.list_devices(conn)]
        if status is not None:
            devices = [d for d in devices if d.status == status]
        return DeviceList(count=len(devices), devices=devices)

    @app.get("/devices/{device_id}", response_model=DeviceStatus, responses=NOT_FOUND)
    def get_device(device_id: DeviceId, conn: Conn):
        device = db.get_device(conn, device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="device not found")
        return with_status(device)

    @app.get("/devices/{device_id}/readings", response_model=DeviceReadings, responses=NOT_FOUND)
    def list_device_readings(device_id: DeviceId, conn: Conn, limit: Limit = 50, anomalies_only: bool = False):
        if db.get_device(conn, device_id) is None:
            raise HTTPException(status_code=404, detail="device not found")
        return DeviceReadings(device_id=device_id, readings=db.list_readings(conn, device_id, limit, anomalies_only))

    @app.get("/anomalies", response_model=AnomalyList)
    def list_anomalies(conn: Conn, limit: Limit = 50):
        return AnomalyList(readings=db.list_anomalies(conn, limit))

    # Operator controls for fault injection. No auth: local experiments only.
    @app.get("/admin/faults", response_model=FaultConfig)
    def get_faults():
        return faults.config

    @app.put("/admin/faults", response_model=FaultConfig)
    def put_faults(config: FaultConfig):
        log.warning("fault config changed", extra={"event": "fault_config_changed", "config": config.model_dump()})
        return faults.set(config)

    @app.delete("/admin/faults", response_model=FaultConfig)
    def delete_faults():
        config = FaultConfig()
        log.warning("fault config reset", extra={"event": "fault_config_changed", "config": config.model_dump()})
        return faults.set(config)

    return app
