"""Pydantic request/response models.

`ReadingIn` enforces the sensor's *physical* limits: anything outside them is
impossible data and is rejected with 422. The *expected* operating range
(anomalies) lives in app/anomaly.py.
"""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

DEVICE_ID_PATTERN = r"^dev-\d{3}$"

DeviceStatusValue = Literal["ok", "anomaly", "stale"]


class ReadingIn(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    device_id: str = Field(pattern=DEVICE_ID_PATTERN, examples=["dev-001"])
    timestamp: AwareDatetime = Field(description="Device clock, with timezone, e.g. 2026-09-26T10:00:00Z")
    temperature_c: float = Field(ge=-40, le=85)
    humidity_pct: float = Field(ge=0, le=100)
    battery_pct: float = Field(ge=0, le=100)


class ReadingOut(BaseModel):
    id: int
    device_id: str
    timestamp: str
    received_at: str
    temperature_c: float
    humidity_pct: float
    battery_pct: float
    is_anomaly: bool
    anomaly_reasons: list[str]


class DeviceStatus(BaseModel):
    device_id: str
    status: DeviceStatusValue
    first_seen: str
    last_seen: str
    reading_count: int
    anomaly_count: int
    latest_reading: ReadingOut


class DeviceList(BaseModel):
    count: int
    devices: list[DeviceStatus]


class DeviceReadings(BaseModel):
    device_id: str
    readings: list[ReadingOut]


class AnomalyList(BaseModel):
    readings: list[ReadingOut]


class ProblemFieldError(BaseModel):
    field: str = Field(description="e.g. humidity_pct, or path.device_id for a path parameter")
    type: str = Field(description="Pydantic error type, e.g. less_than_equal, missing, timestamp_in_future")
    message: str


class Problem(BaseModel):
    """RFC 9457 problem details: the body of every error response (application/problem+json)."""

    type: str = Field(
        description="Stable identifier of the problem kind, e.g. urn:sensor-api:problem:validation-error, "
        ":reading-conflict, :rate-limited, :not-found, :internal-error"
    )
    title: str = Field(description="HTTP status phrase, e.g. Conflict")
    status: int = Field(description="HTTP status code, repeated in the body")
    detail: str = Field(description="What went wrong for this request, in words")
    instance: str = Field(description="Path of the request that failed")
    request_id: str | None = Field(description="Same value as the X-Request-ID response header; search the logs for it")
    errors: list[ProblemFieldError] | None = Field(default=None, description="Field-level errors (422 only)")
