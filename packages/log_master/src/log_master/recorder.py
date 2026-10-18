"""The recording state machine (spec §6.2–6.6).

Global state is `idle` or `recording` (at most one active log). Independently, sources may
carry a pending stop for a previous log. Starting never fails because a source is offline
or unhealthy: such sources are "not joined" and late-join as soon as they are online and
idle. Stopping makes log-master idle immediately; unconfirmed stops are retried on every poll.

All mutations happen under one `asyncio.Lock`, from API calls or from the poll loop.
"""

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable

import httpx
from logserver.errors import ApiError
from logserver.ids import generate_log_id, log_id_timestamp_ns
from logserver.models import Recording

from log_master.client import SourceRequestError, SourceUnreachable, call_source
from log_master.config import Config, ConfigFile
from log_master.models import (
    ActiveLogView,
    Conflict,
    Participant,
    SourceSummary,
    StartRecordingRequest,
    StartRecordingResponse,
    StateSnapshot,
    StopRecordingResponse,
    WarningItem,
)
from log_master.poller import Poller, SourceRuntime
from log_master.state import ActiveLog, PersistedState, StateFile

logger = logging.getLogger(__name__)

ADOPTION_WINDOW_S = 10.0
_MAX_CACHED_RESPONSES = 64
_ACTIVE_STATES = ('armed', 'recording')


def format_bytes(size: float) -> str:
    """Format a byte count with a binary unit, e.g. `4.2 GiB`."""
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if size < 1024 or unit == 'TiB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    raise AssertionError('unreachable')


class Recorder:
    """Coordinates synchronized start/stop, late join, pending stops and adoption.

    Args:
        config_file: The live configuration.
        poller: Provides the latest status of every source.
        http: Client for calls to the sources.
        state_file: Where the active log and pending stops are persisted.
        clock: `CLOCK_REALTIME` nanoseconds; injectable for tests.
    """

    def __init__(
        self,
        config_file: ConfigFile,
        poller: Poller,
        http: httpx.AsyncClient,
        state_file: StateFile,
        clock: Callable[[], int] = time.time_ns,
    ):
        self._config_file = config_file
        self._poller = poller
        self._http = http
        self._state_file = state_file
        self._clock = clock
        self._lock = asyncio.Lock()
        persisted = state_file.load()
        self.active: ActiveLog | None = persisted.active_log
        self.stop_pending: dict[str, str] = dict(persisted.stop_pending)
        self._start_sent: set[str] = set(self.active.participants) if self.active else set()
        self._joined: set[str] = set()
        self._busy: list[str] = []
        self._join_errors: dict[str, str] = {}
        self._takeover_logs: dict[str, str] = {}
        self._first_actual_start: int | None = None
        self._responses: OrderedDict[str, StartRecordingResponse] = OrderedDict()
        self._adoption_deadline = 0.0 if self.active else time.monotonic() + ADOPTION_WINDOW_S
        if self.active is not None:
            logger.info('resuming active log %s from %s', self.active.log_id, state_file.path)

    @property
    def config(self) -> Config:
        """The current configuration."""
        return self._config_file.config

    # ------------------------------------------------------------------ API operations

    async def start(self, request: StartRecordingRequest) -> StartRecordingResponse:
        """Start a new log (`POST /api/v1/recording/start`).

        Raises:
            ApiError: 409 `already_recording`, 409 `conflicts`, 400 `unknown_source` / `no_sources`.
        """
        async with self._lock:
            cached = self._responses.get(request.request_id)
            if cached is not None:
                return cached
            if self.active is not None:
                if self.active.started_request_id == request.request_id:
                    return StartRecordingResponse(active_log=self._active_view(), warnings=self.warnings())
                raise ApiError(
                    409,
                    'already_recording',
                    f'log {self.active.log_id} is being recorded',
                    {'log_id': self.active.log_id},
                )
            names = self._select_sources(request)
            conflicts = self._conflicts(names)
            uncovered = [c for c in conflicts if c.source not in request.takeover]
            if uncovered and not request.skip_busy:
                listing = ', '.join(f'{c.source} ({c.origin} log {c.log_id})' for c in uncovered)
                raise ApiError(
                    409,
                    'conflicts',
                    f'sources busy with other logs: {listing}',
                    {'conflicts': [c.model_dump() for c in uncovered]},
                )
            busy = [c.source for c in uncovered]
            takeovers = [c for c in conflicts if c.source in request.takeover]
            self._adoption_deadline = 0.0
            takeover_warnings = await self._take_over(takeovers)

            now = self._clock()
            start_at = now + int(self.config.recording.start_lead_s * 1e9)
            stop_at = start_at + int(request.duration_s * 1e9) if request.duration_s else None
            self.active = ActiveLog(
                log_id=generate_log_id(now),
                participants=[name for name in names if name not in busy],
                scheduled_start_unix_ns=start_at,
                stop_at_unix_ns=stop_at,
                started_request_id=request.request_id,
            )
            self._busy = busy
            self._joined = set()
            self._start_sent = set()
            self._join_errors = {}
            self._takeover_logs = {c.source: c.log_id for c in takeovers if c.log_id}
            self._first_actual_start = None
            self._persist()
            logger.info(
                'starting log %s with %s', self.active.log_id, ', '.join(self.active.participants) or 'no sources'
            )
            ready = [n for n in self.active.participants if self._poller.online(n) and n not in self.stop_pending]
            await asyncio.gather(*(self._send_start(n, start_at, stop_at) for n in ready))
            response = StartRecordingResponse(
                active_log=self._active_view(), warnings=takeover_warnings + self.warnings()
            )
            self._responses[request.request_id] = response
            while len(self._responses) > _MAX_CACHED_RESPONSES:
                self._responses.popitem(last=False)
            return response

    async def stop(self) -> StopRecordingResponse:
        """Stop the active log (`POST /api/v1/recording/stop`).

        Raises:
            ApiError: 409 `not_recording`.
        """
        async with self._lock:
            if self.active is None:
                raise ApiError(409, 'not_recording', 'no log is being recorded')
            stop_at = self._clock() + int(self.config.recording.stop_lead_s * 1e9)
            return await self._stop_locked(stop_at)

    async def dismiss_pending(self, source: str) -> None:
        """Forget the pending stop of `source`.

        Raises:
            ApiError: 404 `not_pending`.
        """
        async with self._lock:
            if self.stop_pending.pop(source, None) is None:
                raise ApiError(404, 'not_pending', f'no pending stop for source {source!r}')
            logger.info('pending stop of %s dismissed', source)
            self._persist()

    def is_participant(self, name: str) -> bool:
        """Return whether `name` participates in the active log."""
        return self.active is not None and name in self.active.participants

    def forget_source(self, name: str) -> None:
        """Drop all state about a source that was removed from the config."""
        if self.stop_pending.pop(name, None) is not None:
            self._persist()

    # ------------------------------------------------------------------ poll-driven

    async def on_poll(self) -> None:
        """Run after every poll round: adoption, pending-stop retries, timed stop, late join."""
        async with self._lock:
            self._observe()
            await self._maybe_adopt()
            await self._retry_pending_stops()
            if self.active is None:
                return
            now = self._clock()
            stop_at = self.active.stop_at_unix_ns
            if stop_at is not None and now >= stop_at:
                logger.info('log %s reached its stop time', self.active.log_id)
                await self._stop_locked(stop_at)
            else:
                await self._late_join(now)

    def _observe(self) -> None:
        """Track which participants have been seen recording the active log."""
        if self.active is None:
            return
        for name in self.active.participants:
            recording = self._recording(name)
            if recording is not None and recording.log_id == self.active.log_id and recording.state != 'idle':
                self._joined.add(name)
                self._join_errors.pop(name, None)
                actual = recording.actual_start_unix_ns
                if actual is not None and (self._first_actual_start is None or actual < self._first_actual_start):
                    self._first_actual_start = actual

    async def _maybe_adopt(self) -> None:
        """After a restart without state file, adopt a log-master log that sources are still recording."""
        if self.active is not None or time.monotonic() > self._adoption_deadline:
            return
        candidates: dict[str, list[str]] = {}
        for name in self._poller.sources:
            recording = self._recording(name)
            if (
                recording is not None
                and recording.state in _ACTIVE_STATES
                and recording.origin == 'log-master'
                and recording.log_id is not None
                and self.stop_pending.get(name) != recording.log_id
            ):
                candidates.setdefault(recording.log_id, []).append(name)
        if not candidates:
            return
        log_id = max(candidates)
        names = candidates[log_id]
        recordings = [r for r in (self._recording(n) for n in names) if r is not None]
        starts = [r.scheduled_start_unix_ns for r in recordings if r.scheduled_start_unix_ns is not None]
        stops = [r.scheduled_stop_unix_ns for r in recordings if r.scheduled_stop_unix_ns is not None]
        self.active = ActiveLog(
            log_id=log_id,
            participants=names,
            scheduled_start_unix_ns=min(starts) if starts else log_id_timestamp_ns(log_id),
            stop_at_unix_ns=min(stops) if stops else None,
        )
        self._joined = set(names)
        self._start_sent = set(names)
        self._busy = []
        self._join_errors = {}
        self._takeover_logs = {}
        self._first_actual_start = None
        self._adoption_deadline = 0.0
        self._observe()
        self._persist()
        others = sorted(set(candidates) - {log_id})
        logger.warning('adopted log %s recorded by %s', log_id, ', '.join(names))
        if others:
            logger.warning('other log-master logs still recording (not adopted): %s', ', '.join(others))

    async def _retry_pending_stops(self) -> None:
        changed = False
        for name, log_id in list(self.stop_pending.items()):
            runtime = self._poller.sources.get(name)
            if runtime is None:
                del self.stop_pending[name]
                changed = True
                continue
            if not runtime.online or runtime.status is None:
                continue
            recording = runtime.status.recording
            if recording.state == 'idle' or recording.log_id != log_id or await self._send_stop(name, log_id, None):
                logger.info('pending stop of log %s on %s resolved', log_id, name)
                del self.stop_pending[name]
                changed = True
        if changed:
            self._persist()

    async def _late_join(self, now: int) -> None:
        assert self.active is not None
        candidates = [
            name
            for name in self.active.participants
            if (recording := self._recording(name)) is not None
            and recording.state == 'idle'
            and name not in self.stop_pending
        ]
        if not candidates:
            return
        start_at = max(now, self.active.scheduled_start_unix_ns)
        for name in candidates:
            logger.info('late-joining %s to log %s', name, self.active.log_id)
        await asyncio.gather(*(self._send_start(n, start_at, self.active.stop_at_unix_ns) for n in candidates))

    # ------------------------------------------------------------------ helpers

    def _select_sources(self, request: StartRecordingRequest) -> list[str]:
        configured = [source.name for source in self.config.sources]
        if request.sources is None:
            names = [source.name for source in self.config.sources if source.enabled_by_default]
        else:
            unknown = [name for name in request.sources if name not in configured]
            if unknown:
                raise ApiError(400, 'unknown_source', f'unknown sources: {", ".join(unknown)}', {'sources': unknown})
            names = list(dict.fromkeys(request.sources))
        if not names:
            raise ApiError(400, 'no_sources', 'no sources selected')
        return names

    def _conflicts(self, names: list[str]) -> list[Conflict]:
        conflicts = []
        for name in names:
            recording = self._recording(name)
            if (
                recording is not None
                and recording.state in _ACTIVE_STATES
                and recording.log_id != self.stop_pending.get(name)
            ):
                conflicts.append(Conflict(source=name, log_id=recording.log_id, origin=recording.origin))
        return conflicts

    async def _take_over(self, takeovers: list[Conflict]) -> list[WarningItem]:
        """Stop the logs of sources the user chose to take over."""

        async def stop_one(conflict: Conflict) -> WarningItem | None:
            runtime = self._poller.sources[conflict.source]
            logger.info('taking over %s: stopping %s log %s', conflict.source, conflict.origin, conflict.log_id)
            try:
                response = await call_source(
                    self._http,
                    runtime.url,
                    'POST',
                    '/log/stop',
                    json={'log_id': conflict.log_id},
                    timeout=self.config.poll.timeout_s,
                )
            except (SourceUnreachable, SourceRequestError) as exc:
                return WarningItem(
                    code='takeover_failed',
                    level='warn',
                    source=conflict.source,
                    message=f'could not stop log {conflict.log_id} on {conflict.source}: {exc}',
                )
            runtime.update_recording(response.json())
            return None

        results = await asyncio.gather(*(stop_one(c) for c in takeovers))
        return [warning for warning in results if warning is not None]

    async def _send_start(self, name: str, start_at: int, stop_at: int | None) -> None:
        assert self.active is not None
        runtime = self._poller.sources[name]
        self._start_sent.add(name)
        body = {'log_id': self.active.log_id, 'start_at_unix_ns': start_at, 'stop_at_unix_ns': stop_at}
        try:
            response = await call_source(
                self._http, runtime.url, 'POST', '/log/start', json=body, timeout=self.config.poll.timeout_s
            )
        except SourceUnreachable as exc:
            self._join_errors[name] = f'start failed: {exc}'
            return
        except SourceRequestError as exc:
            other = exc.details.get('log_id')
            if exc.code == 'already_recording' and other is not None and other == self._takeover_logs.get(name):
                self._join_errors[name] = f'waiting for log {other} to stop'
            elif exc.code == 'already_recording':
                self._join_errors[name] = f'busy with {exc.details.get("origin")} log {other}'
            else:
                self._join_errors[name] = f'start failed: {exc.message}'
            return
        self._join_errors.pop(name, None)
        self._joined.add(name)
        runtime.update_recording(response.json())

    async def _send_stop(self, name: str, log_id: str, stop_at: int | None) -> bool:
        """Send a stop; return whether the source confirmed it (or is recording a different log)."""
        runtime = self._poller.sources.get(name)
        if runtime is None:
            return True
        try:
            response = await call_source(
                self._http,
                runtime.url,
                'POST',
                '/log/stop',
                json={'log_id': log_id, 'stop_at_unix_ns': stop_at},
                timeout=self.config.poll.timeout_s,
            )
        except SourceUnreachable:
            return False
        except SourceRequestError as exc:
            return exc.code == 'log_id_mismatch'
        runtime.update_recording(response.json())
        return True

    async def _stop_locked(self, stop_at: int) -> StopRecordingResponse:
        active = self.active
        assert active is not None
        targets = [
            name
            for name in active.participants
            if name in self._start_sent or name in self._joined or self._poller.online(name)
        ]
        results = await asyncio.gather(*(self._send_stop(name, active.log_id, stop_at) for name in targets))
        pending = [name for name, confirmed in zip(targets, results, strict=True) if not confirmed]
        for name in pending:
            self.stop_pending[name] = active.log_id
        self.active = None
        self._adoption_deadline = 0.0
        self._persist()
        if pending:
            logger.warning('log %s stopped; stop not confirmed by %s', active.log_id, ', '.join(pending))
        else:
            logger.info('log %s stopped', active.log_id)
        return StopRecordingResponse(log_id=active.log_id, stop_at_unix_ns=stop_at, stop_pending=pending)

    def _persist(self) -> None:
        self._state_file.save(PersistedState(active_log=self.active, stop_pending=self.stop_pending))

    def _recording(self, name: str) -> Recording | None:
        """The latest recording state of an online source, else `None`."""
        runtime = self._poller.sources.get(name)
        if runtime is None or not runtime.online or runtime.status is None:
            return None
        return runtime.status.recording

    # ------------------------------------------------------------------ views

    def _participant(self, name: str, now: int) -> Participant:
        assert self.active is not None
        runtime = self._poller.sources.get(name)
        recording = self._recording(name)
        mine = recording is not None and recording.log_id == self.active.log_id and recording.state != 'idle'
        reason = None
        if name in self._busy:
            status = 'busy'
            reason = 'recording another log; left out'
        elif runtime is None or not runtime.online:
            status = 'lost' if name in self._joined else 'not_joined'
            reason = 'offline'
        elif mine:
            assert recording is not None
            status = 'armed' if recording.state == 'armed' else 'recording'
        elif name in self.stop_pending:
            status = 'not_joined'
            reason = f'waiting for the stop of log {self.stop_pending[name]}'
        elif recording is not None and recording.state != 'idle':
            status = 'busy'
            reason = f'recording {recording.origin} log {recording.log_id}'
            if recording.log_id == self._takeover_logs.get(name):
                status = 'not_joined'
                reason = f'waiting for log {recording.log_id} to stop'
        else:
            status = 'not_joined'
            reason = self._join_errors.get(name, 'joining')
        participant = Participant(name=name, status=status, reason=reason)
        if mine and recording is not None:
            participant.segment_index = recording.segment_index
            participant.bytes_written = recording.bytes_written
            participant.scheduled_start_unix_ns = recording.scheduled_start_unix_ns
            participant.actual_start_unix_ns = recording.actual_start_unix_ns
            scheduled = recording.scheduled_start_unix_ns
            if scheduled is not None and recording.actual_start_unix_ns is not None:
                participant.late_start_ms = (recording.actual_start_unix_ns - scheduled) / 1e6
            elif scheduled is not None and recording.state == 'recording' and now > scheduled:
                participant.late_start_ms = (now - scheduled) / 1e6
        return participant

    def _active_view(self) -> ActiveLogView:
        assert self.active is not None
        now = self._clock()
        names = list(dict.fromkeys(self.active.participants + self._busy))
        participants = [self._participant(name, now) for name in names]
        actual = self._first_actual_start
        for participant in participants:
            if participant.actual_start_unix_ns is not None and (
                actual is None or participant.actual_start_unix_ns < actual
            ):
                actual = participant.actual_start_unix_ns
        return ActiveLogView(
            log_id=self.active.log_id,
            scheduled_start_unix_ns=self.active.scheduled_start_unix_ns,
            actual_start_unix_ns=actual,
            stop_at_unix_ns=self.active.stop_at_unix_ns,
            participants=participants,
        )

    def _low_disk(self, runtime: SourceRuntime) -> bool:
        if runtime.status is None:
            return False
        disk = runtime.status.disk
        limits = self.config.disk
        fraction = disk.free_bytes / disk.total_bytes if disk.total_bytes > 0 else 0.0
        return disk.free_bytes < limits.warn_free_bytes or fraction < limits.warn_free_fraction

    def warnings(self) -> list[WarningItem]:
        """Return all active warnings (spec §6.3, §6.9)."""
        items: list[WarningItem] = []
        late_ms = self.config.recording.late_start_warn_ms
        if self.active is not None:
            for p in self._active_view().participants:
                if p.status == 'not_joined':
                    items.append(
                        WarningItem(
                            code='not_joined',
                            level='warn',
                            source=p.name,
                            message=f'{p.name} has not joined the log ({p.reason})',
                        )
                    )
                elif p.status == 'lost':
                    items.append(
                        WarningItem(
                            code='lost',
                            level='error',
                            source=p.name,
                            message=f'{p.name} went offline during the log; it rejoins when back',
                        )
                    )
                elif p.status == 'busy':
                    items.append(
                        WarningItem(
                            code='busy',
                            level='warn',
                            source=p.name,
                            message=f'{p.name} is not recording this log ({p.reason})',
                        )
                    )
                if p.late_start_ms is not None and p.late_start_ms > late_ms:
                    if p.actual_start_unix_ns is None:
                        message = f'{p.name} has written no data {p.late_start_ms / 1000:.1f} s after its start time'
                    else:
                        message = f'{p.name} started {p.late_start_ms:.0f} ms late'
                    items.append(WarningItem(code='late_start', level='warn', source=p.name, message=message))
        for name, runtime in self._poller.sources.items():
            if runtime.online and runtime.status is not None:
                status = runtime.status
                if status.health.level == 'error':
                    items.append(
                        WarningItem(
                            code='health_error',
                            level='error',
                            source=name,
                            message=f'{name}: {status.health.message or "health error"}',
                        )
                    )
                if self._low_disk(runtime):
                    free = format_bytes(status.disk.free_bytes)
                    items.append(
                        WarningItem(
                            code='low_disk', level='warn', source=name, message=f'{name}: low disk space ({free} free)'
                        )
                    )
                recording = status.recording
                if (
                    recording.state in _ACTIVE_STATES
                    and not self.is_participant(name)
                    and self.stop_pending.get(name) != recording.log_id
                ):
                    items.append(
                        WarningItem(
                            code='foreign_recording',
                            level='warn',
                            source=name,
                            message=f'{name} is recording {recording.origin} log {recording.log_id}, which is not controlled by log-master',
                        )
                    )
            if runtime.info is not None and runtime.info.name != name:
                items.append(
                    WarningItem(
                        code='name_mismatch',
                        level='warn',
                        source=name,
                        message=f'{name} reports its name as {runtime.info.name!r}',
                    )
                )
        items.extend(
            WarningItem(
                code='stop_pending',
                level='warn',
                source=name,
                message=f'stop of log {log_id} not yet confirmed by {name}',
            )
            for name, log_id in self.stop_pending.items()
        )
        items.extend(
            WarningItem(code='restart_required', level='warn', message=f'{path} changed; restart log-master to apply')
            for path in self._config_file.restart_pending
        )
        return items

    def source_summary(self, runtime: SourceRuntime) -> SourceSummary:
        """Return the live summary of one source."""
        log_status = None
        if self.active is not None and (runtime.name in self.active.participants or runtime.name in self._busy):
            log_status = self._participant(runtime.name, self._clock()).status
        elif runtime.name in self.stop_pending:
            log_status = 'stop_pending'
        return SourceSummary(
            name=runtime.name,
            url=runtime.url,
            enabled_by_default=runtime.enabled_by_default,
            online=runtime.online,
            last_seen_unix_ns=runtime.last_seen_unix_ns,
            error=runtime.error,
            info=runtime.info,
            status=runtime.status,
            low_disk=self._low_disk(runtime),
            name_mismatch=runtime.info is not None and runtime.info.name != runtime.name,
            stop_pending=self.stop_pending.get(runtime.name),
            log_status=log_status,
        )

    def snapshot(self) -> StateSnapshot:
        """Return the aggregate state polled by the UI (`GET /api/v1/state`)."""
        return StateSnapshot(
            server_time_unix_ns=self._clock(),
            state='idle' if self.active is None else 'recording',
            active_log=None if self.active is None else self._active_view(),
            sources=[self.source_summary(runtime) for runtime in self._poller.sources.values()],
            stop_pending=dict(self.stop_pending),
            warnings=self.warnings(),
        )
