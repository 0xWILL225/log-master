"""Pydantic models of the log-master REST API (`/api/v1`).

These models are the source of `spec/log-master.openapi.yaml`, from which the frontend's
TypeScript types are generated.
"""

from typing import Any, Literal

from logserver.models import FileEntry, Info, LogState, Origin, ResponseModel, Segment, Status
from pydantic import BaseModel, ConfigDict, Field

ParticipantStatus = Literal['recording', 'armed', 'not_joined', 'busy', 'stop_pending', 'lost']
WarningLevel = Literal['warn', 'error']


class WarningItem(ResponseModel):
    """An active warning shown in the dashboard's warnings panel."""

    code: str = Field(
        description='e.g. `not_joined`, `lost`, `late_start`, `health_error`, `low_disk`, `stop_pending`.'
    )
    level: WarningLevel
    source: str | None = None
    message: str


class Participant(ResponseModel):
    """Status of one source within the active log (spec §6.3)."""

    name: str
    status: ParticipantStatus
    reason: str | None = Field(None, description='Why the source has not joined, if known.')
    segment_index: int | None = None
    bytes_written: int = 0
    scheduled_start_unix_ns: int | None = None
    actual_start_unix_ns: int | None = None
    late_start_ms: float | None = None


class ActiveLogView(ResponseModel):
    """The active log as shown on the dashboard."""

    log_id: str
    scheduled_start_unix_ns: int
    actual_start_unix_ns: int | None = Field(None, description='Earliest actual start over all participants.')
    stop_at_unix_ns: int | None = None
    participants: list[Participant] = Field(description='Participating sources plus sources left out as busy.')


class SourceSummary(ResponseModel):
    """A configured source with its live state."""

    name: str
    url: str
    enabled_by_default: bool
    online: bool
    last_seen_unix_ns: int | None = None
    error: str | None = Field(None, description='Last polling error, if any.')
    info: Info | None = None
    status: Status | None = Field(None, description='Last status received (kept while offline).')
    low_disk: bool = False
    name_mismatch: bool = False
    stop_pending: str | None = Field(None, description='Log ID whose stop is not yet confirmed.')
    log_status: ParticipantStatus | None = Field(None, description='Status within the active log, or `stop_pending`.')


class StateSnapshot(ResponseModel):
    """Aggregate snapshot polled by the UI at 1 Hz (`GET /api/v1/state`)."""

    server_time_unix_ns: int
    state: Literal['idle', 'recording']
    active_log: ActiveLogView | None = None
    sources: list[SourceSummary]
    stop_pending: dict[str, str] = Field(default_factory=dict)
    warnings: list[WarningItem] = Field(default_factory=list)


class SourceList(ResponseModel):
    """Response of `GET /api/v1/sources`."""

    sources: list[SourceSummary]


class StartRecordingRequest(BaseModel):
    """Body of `POST /api/v1/recording/start`."""

    model_config = ConfigDict(extra='forbid')

    request_id: str = Field(
        min_length=1, max_length=128, description='Client-generated; repeats return the original response.'
    )
    sources: list[str] | None = Field(None, description='Defaults to all sources with `enabled_by_default`.')
    duration_s: float | None = Field(None, gt=0)
    takeover: list[str] = Field(default_factory=list, description='Busy sources to stop and include.')
    skip_busy: bool = Field(False, description='Leave out busy sources not listed in `takeover`.')


class Conflict(ResponseModel):
    """A selected source that is recording a log log-master does not own."""

    source: str
    log_id: str | None
    origin: Origin | None


class ConflictDetails(ResponseModel):
    """Details of a `409 conflicts` error."""

    conflicts: list[Conflict]


class StartErrorBody(ResponseModel):
    """Error body of `POST /api/v1/recording/start` (`conflicts` or `already_recording`)."""

    code: str
    message: str
    details: ConflictDetails | dict[str, Any]


class StartErrorEnvelope(ResponseModel):
    """Error envelope of `POST /api/v1/recording/start`."""

    error: StartErrorBody


class StartRecordingResponse(ResponseModel):
    """Response of `POST /api/v1/recording/start`."""

    active_log: ActiveLogView
    warnings: list[WarningItem] = Field(default_factory=list)


class StopRecordingResponse(ResponseModel):
    """Response of `POST /api/v1/recording/stop`."""

    log_id: str
    stop_at_unix_ns: int
    stop_pending: list[str] = Field(default_factory=list, description='Sources that did not confirm the stop.')


class MergedLogPart(ResponseModel):
    """One source's part of a log."""

    source: str
    origin: Origin | None = None
    state: LogState
    start_unix_ns: int | None = None
    end_unix_ns: int | None = None
    segments: list[Segment] = Field(default_factory=list)
    files: list[FileEntry] = Field(default_factory=list)
    total_bytes: int = 0
    manifest: dict[str, Any] | None = Field(None, description='Only in `GET /api/v1/logs/{id}`.')


class MergedLog(ResponseModel):
    """A log merged over all sources that have a part of it."""

    log_id: str
    start_unix_ns: int | None = None
    end_unix_ns: int | None = None
    duration_ns: int | None = None
    total_bytes: int = 0
    recording: bool = Field(description='Any part is still recording.')
    incomplete: bool = Field(description='Any part is interrupted, or a participant of the active log has no part.')
    standalone: bool = Field(description='Any part was started standalone.')
    gaps: bool = Field(description='Any part has more than one segment.')
    missing_sources: list[str] = Field(
        default_factory=list, description='Participants of the active log without a part.'
    )
    parts: list[MergedLogPart]


class LogListResponse(ResponseModel):
    """Response of `GET /api/v1/logs`."""

    logs: list[MergedLog]
    unreachable_sources: list[str] = Field(default_factory=list)


class LogResponse(ResponseModel):
    """Response of `GET /api/v1/logs/{id}`."""

    log: MergedLog
    unreachable_sources: list[str] = Field(default_factory=list)
