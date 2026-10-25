from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class SubmitReadingRequest(_message.Message):
    __slots__ = ("device_id", "timestamp", "temperature_c", "humidity_pct", "battery_pct")
    DEVICE_ID_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    TEMPERATURE_C_FIELD_NUMBER: _ClassVar[int]
    HUMIDITY_PCT_FIELD_NUMBER: _ClassVar[int]
    BATTERY_PCT_FIELD_NUMBER: _ClassVar[int]
    device_id: str
    timestamp: str
    temperature_c: float
    humidity_pct: float
    battery_pct: float
    def __init__(self, device_id: _Optional[str] = ..., timestamp: _Optional[str] = ..., temperature_c: _Optional[float] = ..., humidity_pct: _Optional[float] = ..., battery_pct: _Optional[float] = ...) -> None: ...

class Reading(_message.Message):
    __slots__ = ("id", "device_id", "timestamp", "received_at", "temperature_c", "humidity_pct", "battery_pct", "is_anomaly", "anomaly_reasons")
    ID_FIELD_NUMBER: _ClassVar[int]
    DEVICE_ID_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    RECEIVED_AT_FIELD_NUMBER: _ClassVar[int]
    TEMPERATURE_C_FIELD_NUMBER: _ClassVar[int]
    HUMIDITY_PCT_FIELD_NUMBER: _ClassVar[int]
    BATTERY_PCT_FIELD_NUMBER: _ClassVar[int]
    IS_ANOMALY_FIELD_NUMBER: _ClassVar[int]
    ANOMALY_REASONS_FIELD_NUMBER: _ClassVar[int]
    id: int
    device_id: str
    timestamp: str
    received_at: str
    temperature_c: float
    humidity_pct: float
    battery_pct: float
    is_anomaly: bool
    anomaly_reasons: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, id: _Optional[int] = ..., device_id: _Optional[str] = ..., timestamp: _Optional[str] = ..., received_at: _Optional[str] = ..., temperature_c: _Optional[float] = ..., humidity_pct: _Optional[float] = ..., battery_pct: _Optional[float] = ..., is_anomaly: _Optional[bool] = ..., anomaly_reasons: _Optional[_Iterable[str]] = ...) -> None: ...

class SubmitReadingResponse(_message.Message):
    __slots__ = ("reading", "replayed")
    READING_FIELD_NUMBER: _ClassVar[int]
    REPLAYED_FIELD_NUMBER: _ClassVar[int]
    reading: Reading
    replayed: bool
    def __init__(self, reading: _Optional[_Union[Reading, _Mapping]] = ..., replayed: _Optional[bool] = ...) -> None: ...
