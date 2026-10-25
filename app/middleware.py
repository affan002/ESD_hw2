"""ASGI middleware: assigns a request ID and writes one access-log line per request."""

import json
import logging
import re
import time
import uuid

from app import metrics
from app.problems import MEDIA_TYPE, problem_body, slug_for
from app.logging_setup import request_id_var

log = logging.getLogger("app.http")

REQUEST_ID_HEADER = b"x-request-id"
VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _incoming_request_id(scope) -> str:
    for name, value in scope.get("headers", []):
        if name == REQUEST_ID_HEADER:
            candidate = value.decode("latin-1")
            if VALID_REQUEST_ID.match(candidate):
                return candidate
            break
    return uuid.uuid4().hex


class RequestContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope)
        token = request_id_var.set(request_id)
        base_root_path = scope.get("root_path", "")
        status_code = 500
        response_started = False
        start = time.perf_counter()
        metrics.HTTP_IN_PROGRESS.inc()

        async def send_wrapper(message):
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                headers = list(message.get("headers", []))
                headers.append((REQUEST_ID_HEADER, request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            # Handle crashes here rather than in Starlette's outer error
            # middleware, so the log line and the 500 response keep the request ID.
            log.error(
                "unhandled exception",
                exc_info=True,
                extra={"event": "unhandled_exception", "error": type(exc).__name__},
            )
            if not response_started:
                await send_wrapper(
                    {
                        "type": "http.response.start",
                        "status": 500,
                        "headers": [(b"content-type", MEDIA_TYPE.encode())],
                    }
                )
                body = problem_body(500, slug_for(500), "internal server error", scope.get("path", ""))
                await send_wrapper({"type": "http.response.body", "body": json.dumps(body).encode()})
        finally:
            duration_s = time.perf_counter() - start
            duration_ms = round(duration_s * 1000, 2)
            # The router stores the matched route in scope; use its template
            # (e.g. /devices/{device_id}) so `path` stays a small, bounded set.
            # Mounted apps (e.g. /static) don't set a route but extend root_path.
            route = scope.get("route")
            mount_prefix = scope.get("root_path", "")[len(base_root_path):]
            path = getattr(route, "path", None) or mount_prefix or "unmatched"

            metrics.HTTP_IN_PROGRESS.dec()
            metrics.HTTP_REQUESTS.labels(scope["method"], path, str(status_code)).inc()
            metrics.HTTP_DURATION.labels(scope["method"], path).observe(duration_s)

            log.log(
                logging.ERROR if status_code >= 500 else logging.INFO,
                "%s %s -> %s",
                scope["method"],
                path,
                status_code,
                extra={
                    "event": "http_request",
                    "method": scope["method"],
                    "path": path,
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                },
            )
            request_id_var.reset(token)
