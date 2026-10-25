"""gRPC server for the "submit a reading" task: python -m app.grpc_server

A separate process (Compose service `grpc`, port 50051) that applies the same
business rule as HTTP POST /readings (app/ingest.py) to the same SQLite database.
It is not a wrapper around the HTTP API: it never calls it.

Call path: client stub (IngestServiceStub, generated) → channel → this server →
ObservabilityInterceptor (request ID, access log, metrics) → IngestServicer.SubmitReading
→ ingest.submit_reading → SQLite.
"""

import logging
import re
import signal
import time
import uuid
from concurrent import futures

import grpc
from prometheus_client import start_http_server
from pydantic import ValidationError

from app import db, ingest, metrics
from app.config import Settings, load_settings
from app.grpc_gen import sensor_ingest_pb2 as pb
from app.grpc_gen import sensor_ingest_pb2_grpc as pb_grpc
from app.logging_setup import request_id_var, setup_logging
from app.middleware import VALID_REQUEST_ID
from app.models import DEVICE_ID_PATTERN, ReadingIn, ReadingOut
from app.ratelimit import RateLimiter

log = logging.getLogger("app.grpc")

REQUEST_ID_KEY = "x-request-id"  # gRPC metadata keys are lowercase
_DEVICE_ID_RE = re.compile(DEVICE_ID_PATTERN)
_NUMERIC_FIELDS = ("temperature_c", "humidity_pct", "battery_pct")


class ObservabilityInterceptor(grpc.ServerInterceptor):
    """The gRPC counterpart of RequestContextMiddleware: for every unary call it takes
    (or creates) the request ID, echoes it back in the response metadata, and records
    one `grpc_request` log line plus the gRPC metrics."""

    def intercept_service(self, continuation, handler_call_details):
        handler = continuation(handler_call_details)
        if handler is None or handler.unary_unary is None:
            return handler  # unknown method: gRPC answers UNIMPLEMENTED itself
        method = handler_call_details.method.rsplit("/", 1)[-1]  # "/sensor.v1.IngestService/SubmitReading"
        incoming = dict(handler_call_details.invocation_metadata or ()).get(REQUEST_ID_KEY, "")
        inner = handler.unary_unary

        def wrapper(request, context):
            request_id = incoming if VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
            token = request_id_var.set(request_id)
            context.send_initial_metadata(((REQUEST_ID_KEY, request_id),))
            metrics.GRPC_IN_PROGRESS.labels(method).inc()
            start = time.perf_counter()
            try:
                return inner(request, context)
            except Exception as exc:
                if context.code() is None:  # not an abort(): a real crash
                    log.error("unhandled exception", exc_info=True,
                              extra={"event": "unhandled_exception", "error": type(exc).__name__})
                    context.abort(grpc.StatusCode.INTERNAL, "internal server error")
                raise
            finally:
                duration_s = time.perf_counter() - start
                code = (context.code() or grpc.StatusCode.OK).name
                metrics.GRPC_IN_PROGRESS.labels(method).dec()
                metrics.GRPC_REQUESTS.labels(method, code).inc()
                metrics.GRPC_DURATION.labels(method).observe(duration_s)
                extra = {"event": "grpc_request", "rpc_method": method, "grpc_code": code,
                         "duration_ms": round(duration_s * 1000, 2)}
                if code != "OK":
                    extra["grpc_details"] = context.details()
                log.log(logging.ERROR if code in ("INTERNAL", "UNKNOWN") else logging.INFO,
                        "%s -> %s", method, code, extra=extra)
                request_id_var.reset(token)

        return grpc.unary_unary_rpc_method_handler(
            wrapper,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )


def _to_pb(r: ReadingOut) -> pb.Reading:
    return pb.Reading(
        id=r.id, device_id=r.device_id, timestamp=r.timestamp, received_at=r.received_at,
        temperature_c=r.temperature_c, humidity_pct=r.humidity_pct, battery_pct=r.battery_pct,
        is_anomaly=r.is_anomaly, anomaly_reasons=r.anomaly_reasons,
    )


def _describe(errors: list[dict]) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in errors)


class IngestServicer(pb_grpc.IngestServiceServicer):
    """Implements the generated IngestServiceServicer base class."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.limiter = RateLimiter(settings.rate_limit_burst, settings.rate_limit_per_s)

    def SubmitReading(self, request: pb.SubmitReadingRequest, context: grpc.ServicerContext) -> pb.SubmitReadingResponse:
        # Only fields that were actually sent; a missing number is a validation error, not 0.0.
        data = {"device_id": request.device_id, "timestamp": request.timestamp}
        data.update({f: getattr(request, f) for f in _NUMERIC_FIELDS if request.HasField(f)})
        device_id = request.device_id if _DEVICE_ID_RE.match(request.device_id) else None
        try:
            reading = ReadingIn.model_validate(data)  # the same model, so the same physical limits, as HTTP
        except ValidationError as exc:
            errors = [{"loc": e["loc"], "type": e["type"], "msg": e["msg"]} for e in exc.errors()]
            ingest.record_rejection(errors, device_id, "grpc")
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, _describe(errors))

        retry_after = self.limiter.check(reading.device_id, "SubmitReading")
        if retry_after is not None:
            context.set_trailing_metadata((("retry-after", str(retry_after)),))
            context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED,
                          f"device {reading.device_id} is over its rate limit; retry in {retry_after} s")

        conn = db.connect(self.settings.db_path)
        try:
            result = ingest.submit_reading(conn, reading, self.settings, transport="grpc")
        except ingest.FutureTimestamp as exc:
            ingest.record_rejection([{"loc": ("timestamp",), "type": exc.type}], reading.device_id, "grpc")
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"timestamp: {exc}")
        except ingest.ReadingConflict as exc:
            context.abort(grpc.StatusCode.ALREADY_EXISTS, str(exc))
        finally:
            conn.close()
        return pb.SubmitReadingResponse(reading=_to_pb(result.reading), replayed=not result.created)


def serve(settings: Settings, port: int | None = None) -> tuple[grpc.Server, int]:
    """Start the server (non-blocking). Returns it and the bound port (port=0 picks a free one)."""
    db.init_schema(settings.db_path)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10), interceptors=[ObservabilityInterceptor()])
    pb_grpc.add_IngestServiceServicer_to_server(IngestServicer(settings), server)
    bound = server.add_insecure_port(f"{settings.api_host}:{settings.grpc_port if port is None else port}")
    server.start()
    return server, bound


def main() -> None:
    settings = load_settings()
    setup_logging("sensor-grpc", settings.log_level)
    start_http_server(settings.grpc_metrics_port)  # /metrics for Prometheus (job sensor-grpc)
    server, port = serve(settings)
    log.info("sensor-grpc starting", extra={"event": "startup", "config": {
        "db_path": settings.db_path, "grpc_port": port, "metrics_port": settings.grpc_metrics_port}})

    def stop(signum, _frame):
        log.info("sensor-grpc stopping", extra={"event": "shutdown"})
        server.stop(grace=5)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    server.wait_for_termination()


if __name__ == "__main__":
    main()
