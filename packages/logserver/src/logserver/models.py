"""Pydantic models of the Logging Server API (protocol version 1).

These models are the single source of truth for ``spec/logserver.openapi.yaml``. Every
request and response body of the ``/v1`` endpoints is defined here, as is the on-disk
``manifest.json`` format.

All times are integer nanoseconds since the Unix epoch (UTC, ``CLOCK_REALTIME``) and all
sizes are bytes.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION: int = 1
MANIFEST_VERSION: int = 1

LOG_ID_PATTERN: str = r'^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z_[0-9a-f]{6}$'

HealthLevel = Literal['ok', 'warn', 'error']
RecordingState = Literal['idle', 'armed', 'recording', 'stopping']
Origin = Literal['log-master', 'standalone']
LogState = Literal['recording', 'complete', 'interrupted']
DetailValue = bool | int | float | str | None


class RequestModel(BaseModel):
    """Base class for request bodies: unknown fields are rejected."""

    model_config = ConfigDict(extra='forbid')


class ResponseModel(BaseModel):
    """Base class for response bodies: fields with defaults are always present, so the schema marks them required."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class ErrorBody(ResponseModel):
    """Machine-readable error description."""

    code: str = Field(description='Stable error code, e.g. `already_recording`.')
    message: str = Field(description='Human-readable reason.')
    details: dict[str, Any] = Field(default_factory=dict, description='Code-specific extra information.')


class ErrorEnvelope(ResponseModel):
    """Body of every non-2xx response."""

    error: ErrorBody


class Info(ResponseModel):
    """Static identity of a logging server (`GET /v1/info`)."""

    protocol_version: int = PROTOCOL_VERSION
    name: str = Field(description='Informational name; the name configured in log-master is authoritative.')
    source_type: str = Field(description='Kind of data source, e.g. `septentrio-mosaic-h`.')
    server_version: str
    hostname: str
    log_directory: str


class Health(ResponseModel):
    """Overall health as judged by the server itself. Never blocks recording."""

    level: HealthLevel = 'ok'
    message: str | None = None


class Recording(ResponseModel):
    """Recording state of a server. When `state` is `idle`, all other fields are null / 0."""

    state: RecordingState = 'idle'
    log_id: str | None = None
    origin: Origin | None = None
    scheduled_start_unix_ns: int | None = Field(None, description='Start time of the current segment request.')
    actual_start_unix_ns: int | None = Field(
        None, description='Host arrival time of the first unit written in the current segment.'
    )
    scheduled_stop_unix_ns: int | None = None
    segment_index: int | None = None
    bytes_written: int = Field(0, description='Bytes written in the current segment.')


class Disk(ResponseModel):
    """Free space of the file system holding the log directory."""

    path: str
    free_bytes: int
    total_bytes: int


class Detail(ResponseModel):
    """One row of the generic, self-describing status key/value card."""

    key: str
    label: str
    value: DetailValue = None
    unit: str | None = None
    level: HealthLevel | None = None


class Status(ResponseModel):
    """Cheap status snapshot (`GET /v1/status`), polled by log-master at about 1 Hz."""

    health: Health
    recording: Recording
    disk: Disk
    details: list[Detail] = Field(default_factory=list)


class StartRequest(RequestModel):
    """Body of `POST /v1/log/start`. All fields are optional."""

    log_id: str | None = Field(
        None, pattern=LOG_ID_PATTERN, description='Omit to start a standalone log with a server-generated ID.'
    )
    start_at_unix_ns: int | None = Field(None, description='Omit to start immediately.')
    stop_at_unix_ns: int | None = Field(None, description='If set, the server stops by itself at this time.')


class StopRequest(RequestModel):
    """Body of `POST /v1/log/stop`. All fields are optional."""

    log_id: str | None = Field(
        None, pattern=LOG_ID_PATTERN, description='If set and different from the current log: 409 log_id_mismatch.'
    )
    stop_at_unix_ns: int | None = Field(None, description='Omit (or pass a past time) to stop immediately.')


class Segment(ResponseModel):
    """A contiguous recording period of one source within one log."""

    index: int
    start_unix_ns: int | None = Field(None, description='Arrival time of the first written unit.')
    end_unix_ns: int | None = None
    ended_cleanly: bool = False
    files: list[str] = Field(default_factory=list)


class FileEntry(ResponseModel):
    """A file inside a log directory."""

    path: str = Field(description='Path relative to the log directory, `/`-separated.')
    size: int
    mtime_unix_ns: int


class ManifestSource(ResponseModel):
    """Identity of the server that wrote a manifest."""

    name: str
    source_type: str
    server_version: str
    hostname: str


class Manifest(ResponseModel):
    """Contents of `<log_directory>/<log_id>/manifest.json`."""

    manifest_version: int = MANIFEST_VERSION
    log_id: str
    origin: Origin
    state: LogState
    source: ManifestSource
    scheduled_start_unix_ns: int | None = None
    scheduled_stop_unix_ns: int | None = None
    segments: list[Segment] = Field(default_factory=list)


class LogEntry(ResponseModel):
    """One log as listed by `GET /v1/logs`."""

    log_id: str
    origin: Origin | None = Field(None, description='Null only if the manifest is missing or unreadable.')
    state: LogState
    start_unix_ns: int | None = None
    end_unix_ns: int | None = None
    segments: list[Segment] = Field(default_factory=list)
    files: list[FileEntry] = Field(default_factory=list)
    total_bytes: int = 0


class LogList(ResponseModel):
    """Response of `GET /v1/logs`, newest first."""

    logs: list[LogEntry]


class LogDetail(LogEntry):
    """Response of `GET /v1/logs/{log_id}`: a log entry plus its full manifest."""

    manifest: dict[str, Any] | None = None


class ConfigDocument(ResponseModel):
    """Response of `GET /v1/config` and `PATCH /v1/config`."""

    model_config = ConfigDict(
        populate_by_name=True, serialize_by_alias=True, json_schema_serialization_defaults_required=True
    )

    json_schema: dict[str, Any] = Field(alias='schema', description='JSON Schema (draft 2020-12) of `values`.')
    values: dict[str, Any] = Field(description='Current values; `writeOnly` properties are reported as null.')


class ActionSpec(ResponseModel):
    """Self-description of a device-specific action."""

    name: str = Field(pattern=r'^[a-z0-9][a-z0-9_-]*$')
    label: str
    description: str | None = None
    params_schema: dict[str, Any] = Field(default_factory=lambda: {'type': 'object', 'properties': {}})
    available: bool = True
    unavailable_reason: str | None = None
    confirm: str | None = Field(None, description='If set, clients ask the user to confirm this text first.')


class ActionList(ResponseModel):
    """Response of `GET /v1/actions`."""

    actions: list[ActionSpec]


class ActionResult(ResponseModel):
    """Response of `POST /v1/actions/{name}`."""

    message: str
    data: dict[str, Any] = Field(default_factory=dict)
