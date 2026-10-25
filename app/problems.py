"""RFC 9457 problem details: one error format for every error response.

Every error (404, 405, 409, 422, 429, 500) is returned as `application/problem+json`
with the same fields, so callers can branch on `status`/`type` and quote `request_id`.
"""

from http import HTTPStatus

from fastapi.responses import JSONResponse

from app.logging_setup import request_id_var

MEDIA_TYPE = "application/problem+json"


class ProblemError(Exception):
    """Raise from a route to return a problem response with a specific type."""

    def __init__(self, status: int, slug: str, detail: str, headers: dict[str, str] | None = None):
        super().__init__(detail)
        self.status = status
        self.slug = slug
        self.detail = detail
        self.headers = headers


def slug_for(status: int) -> str:
    return HTTPStatus(status).phrase.lower().replace(" ", "-").replace("'", "")


def problem_body(status: int, slug: str, detail: str, instance: str, errors: list[dict] | None = None) -> dict:
    body = {
        "type": f"urn:sensor-api:problem:{slug}",
        "title": HTTPStatus(status).phrase,
        "status": status,
        "detail": detail,
        "instance": instance,
        "request_id": request_id_var.get(),
    }
    if errors is not None:
        body["errors"] = errors
    return body


def problem_response(
    status: int,
    slug: str,
    detail: str,
    instance: str,
    errors: list[dict] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        problem_body(status, slug, detail, instance, errors),
        status_code=status,
        media_type=MEDIA_TYPE,
        headers=headers,
    )
