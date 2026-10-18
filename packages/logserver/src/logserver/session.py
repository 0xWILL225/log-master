"""Log directory, manifest lifecycle, segment numbering, rejoin and start/stop gating.

:class:`LogSession` owns everything about logs on disk so that a logging server only has
to read data units from its device and write them to files.

Threading model
---------------
A server has two kinds of threads that touch the session:

* **HTTP threads** (the uvicorn worker pool) call :meth:`LogSession.start`,
  :meth:`LogSession.stop`, :meth:`LogSession.recording` and the listing methods. These only
  change the session's *intent* (which log, which start/stop times) and never call the
  backend's segment callbacks.
* **The I/O thread** (the server's own data loop) calls :meth:`LogSession.should_write`,
  :meth:`LogSession.on_written`, :meth:`LogSession.tick`, :meth:`LogSession.write_failed`
  and :meth:`LogSession.close`. All segment transitions (opening files, closing them at the
  stop time, finalizing the manifest) happen inside these calls, so the backend's
  :meth:`SegmentHandler.open_segment` / :meth:`SegmentHandler.close_segment` always run on
  the I/O thread and never race with its writes.

The I/O loop must call :meth:`LogSession.tick` regularly (at least a few times per second)
even when no data arrives, so that timed stops and periodic manifest refreshes happen.

An internal lock protects the session state. It is only held for state snapshots; disk
I/O (manifest writes, backend callbacks) happens outside it.

Typical I/O loop::

    while running:
        session.tick()
        unit, arrival_unix_ns = read_unit()          # with a timeout
        if unit is not None and session.should_write(arrival_unix_ns):
            try:
                data_file.write(unit)
            except OSError as exc:
                session.write_failed(str(exc))
            else:
                session.on_written(len(unit), arrival_unix_ns)
"""

import logging
import os
import re
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from logserver.errors import ApiError
from logserver.fsutil import atomic_write_bytes
from logserver.ids import generate_log_id, is_valid_log_id
from logserver.models import (
    Disk,
    FileEntry,
    LogDetail,
    LogEntry,
    Manifest,
    ManifestSource,
    Origin,
    Recording,
    Segment,
    StartRequest,
    StopRequest,
)

logger = logging.getLogger(__name__)

MANIFEST_NAME: str = 'manifest.json'
_MANIFEST_TMP_NAME: str = MANIFEST_NAME + '.tmp'
_SEGMENT_NUMBER_RE = re.compile(r'\.(\d{3,})\.')


class SegmentHandler(Protocol):
    """Callbacks through which a :class:`LogSession` asks the backend to open/close data files."""

    def open_segment(self, session: 'LogSession') -> None:
        """Open the data files of a new segment, using :meth:`LogSession.segment_path` for their paths.

        Called on the I/O thread. May raise `OSError`, which ends the segment uncleanly.
        """
        ...

    def close_segment(self, session: 'LogSession', clean: bool) -> None:
        """Flush and close the data files of the current segment.

        Called on the I/O thread. `clean` is false after a write failure. Also called after an
        `open_segment` that raised, so partially opened files can be closed. May raise `OSError`,
        which marks the segment as not cleanly ended.
        """
        ...


def read_manifest(log_dir: Path) -> Manifest:
    """Read and validate `<log_dir>/manifest.json`.

    Raises:
        OSError: If the file cannot be read.
        ValueError: If the file is not a valid manifest.
    """
    return Manifest.model_validate_json((log_dir / MANIFEST_NAME).read_bytes())


def write_manifest(log_dir: Path, manifest: Manifest) -> None:
    """Atomically write `<log_dir>/manifest.json`."""
    atomic_write_bytes(log_dir / MANIFEST_NAME, manifest.model_dump_json(indent=2).encode() + b'\n')


def list_files(log_dir: Path) -> list[FileEntry]:
    """List the regular files below `log_dir` (symlinks and temporary manifests excluded), sorted by path."""
    entries: list[FileEntry] = []
    for root, dirs, files in os.walk(log_dir, followlinks=False):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for name in files:
            full = Path(root) / name
            if full.is_symlink() or (Path(root) == log_dir and name == _MANIFEST_TMP_NAME):
                continue
            try:
                stat = full.stat()
            except FileNotFoundError:
                continue
            rel = PurePosixPath(full.relative_to(log_dir).as_posix())
            entries.append(FileEntry(path=str(rel), size=stat.st_size, mtime_unix_ns=stat.st_mtime_ns))
    entries.sort(key=lambda entry: entry.path)
    return entries


@dataclass
class _ActiveLog:
    """Mutable state of the log a session is currently recording."""

    log_dir: Path
    manifest: Manifest
    start_at_ns: int
    stop_at_ns: int | None
    stop_requested: bool = False
    segment_open: bool = False
    segment_ended: bool = False
    finalizing: bool = False
    bytes_written: int = 0
    last_written_ns: int | None = None
    last_manifest_write_ns: int = 0
    manifest_dirty: bool = False

    @property
    def segment(self) -> Segment:
        """The current (last) segment of the manifest."""
        return self.manifest.segments[-1]


class LogSession:
    """Manages one server's log directory and its (single) active log.

    Args:
        log_directory: Directory holding one subdirectory per log ID. Created if missing.
        source: Identity written into every manifest.
        handler: Receives open/close callbacks for segment data files (usually the backend).
        clock: Source of `CLOCK_REALTIME` nanoseconds; injectable for tests.
        manifest_refresh_s: Interval of periodic manifest rewrites while recording.
        stop_grace_s: When no data is flowing, :meth:`tick` finalizes a log this long after
            its stop time, so units that arrived just before the stop can still be written.
    """

    def __init__(
        self,
        log_directory: Path | str,
        source: ManifestSource,
        handler: SegmentHandler,
        *,
        clock: Callable[[], int] = time.time_ns,
        manifest_refresh_s: float = 10.0,
        stop_grace_s: float = 0.2,
    ):
        self._directory = Path(log_directory).absolute()
        self._directory.mkdir(parents=True, exist_ok=True)
        self._source = source
        self._handler = handler
        self._clock = clock
        self._refresh_ns = int(manifest_refresh_s * 1e9)
        self._grace_ns = int(stop_grace_s * 1e9)
        self._lock = threading.RLock()
        self._control_lock = threading.Lock()
        self._active: _ActiveLog | None = None
        self._fault: str | None = None
        self._recover_interrupted()

    # ------------------------------------------------------------------ properties

    @property
    def log_directory(self) -> Path:
        """The directory holding all logs of this server."""
        return self._directory

    @property
    def active_log_id(self) -> str | None:
        """ID of the log being recorded (armed, recording or stopping), or `None` when idle."""
        with self._lock:
            return None if self._active is None else self._active.manifest.log_id

    @property
    def fault(self) -> str | None:
        """Description of the last write failure; cleared when a new log starts."""
        with self._lock:
            return self._fault

    def is_recording(self, log_id: str) -> bool:
        """Return whether `log_id` is the log currently being recorded."""
        return self.active_log_id == log_id

    # ------------------------------------------------------------------ HTTP-thread API

    def start(self, request: StartRequest) -> Recording:
        """Handle `POST /v1/log/start`.

        Creates the log directory and manifest (or opens the next segment of an existing log,
        i.e. rejoins) and arms the session. The data files are opened by the I/O thread on its
        next :meth:`tick` / :meth:`should_write`.

        Raises:
            ApiError: 409 `already_recording` if a different log is active, 400 `bad_request`
                for an invalid log ID or stop time.
        """
        with self._control_lock:
            now = self._clock()
            with self._lock:
                active = self._active
                if active is not None:
                    if request.log_id is not None and request.log_id == active.manifest.log_id:
                        return self._recording_locked(now)
                    raise ApiError(
                        409,
                        'already_recording',
                        f'already recording log {active.manifest.log_id}',
                        {'log_id': active.manifest.log_id, 'origin': active.manifest.origin},
                    )

            origin: Origin = 'standalone' if request.log_id is None else 'log-master'
            log_id = request.log_id if request.log_id is not None else generate_log_id(now)
            if not is_valid_log_id(log_id):
                raise ApiError(400, 'bad_request', f'invalid log ID: {log_id!r}')
            start_at = request.start_at_unix_ns if request.start_at_unix_ns is not None else now
            stop_at = request.stop_at_unix_ns
            if stop_at is not None and stop_at <= start_at:
                raise ApiError(400, 'bad_request', 'stop_at_unix_ns must be later than the start time')

            log_dir = self._directory / log_id
            manifest = self._prepare_manifest(log_dir, log_id, origin, start_at, stop_at)
            write_manifest(log_dir, manifest)
            with self._lock:
                self._active = _ActiveLog(
                    log_dir=log_dir,
                    manifest=manifest,
                    start_at_ns=start_at,
                    stop_at_ns=stop_at,
                    last_manifest_write_ns=self._clock(),
                )
                self._fault = None
                logger.info('log %s segment %d armed', log_id, manifest.segments[-1].index)
                return self._recording_locked(now)

    def stop(self, request: StopRequest) -> Recording:
        """Handle `POST /v1/log/stop`.

        Schedules the stop; the I/O thread closes the files and finalizes the manifest once the
        stop time is reached. A stop never extends a log that already has an earlier stop time.

        Raises:
            ApiError: 409 `log_id_mismatch` if `request.log_id` is not the active log.
        """
        with self._control_lock, self._lock:
            now = self._clock()
            active = self._active
            if active is None:
                return Recording()
            if request.log_id is not None and request.log_id != active.manifest.log_id:
                raise ApiError(
                    409,
                    'log_id_mismatch',
                    f'recording log {active.manifest.log_id}, not {request.log_id}',
                    {'log_id': active.manifest.log_id, 'origin': active.manifest.origin},
                )
            requested = request.stop_at_unix_ns
            stop_at = requested if requested is not None and requested > now else now
            if active.stop_at_ns is None or stop_at < active.stop_at_ns:
                active.stop_at_ns = stop_at
                active.manifest.scheduled_stop_unix_ns = stop_at
            active.stop_requested = True
            return self._recording_locked(now)

    def recording(self) -> Recording:
        """Return the `recording` object of `GET /v1/status`."""
        with self._lock:
            return self._recording_locked(self._clock())

    def disk(self) -> Disk:
        """Return free/total space of the file system holding the log directory."""
        usage = shutil.disk_usage(self._directory)
        return Disk(path=str(self._directory), free_bytes=usage.free, total_bytes=usage.total)

    def list_logs(self) -> list[LogEntry]:
        """List all logs in the log directory, newest first (`GET /v1/logs`)."""
        log_ids = sorted(
            (
                p.name
                for p in self._directory.iterdir()
                if p.is_dir() and not p.is_symlink() and is_valid_log_id(p.name)
            ),
            reverse=True,
        )
        return [
            LogEntry.model_validate(self._log_detail(log_id).model_dump(exclude={'manifest'})) for log_id in log_ids
        ]

    def get_log(self, log_id: str) -> LogDetail:
        """Return one log including its manifest (`GET /v1/logs/{log_id}`).

        Raises:
            ApiError: 404 `log_not_found`.
        """
        self._log_dir_or_404(log_id)
        return self._log_detail(log_id)

    def resolve_file(self, log_id: str, path: str) -> Path:
        """Resolve a file path inside a log directory for download.

        Raises:
            ApiError: 404 `log_not_found` / `file_not_found`, also for any path that escapes the
                log directory (`..`, absolute paths, symlinks pointing outside).
        """
        log_dir = self._log_dir_or_404(log_id).resolve()
        target = (log_dir / path).resolve()
        if not target.is_relative_to(log_dir) or not target.is_file() or target.name == _MANIFEST_TMP_NAME:
            raise ApiError(404, 'file_not_found', f'no file {path!r} in log {log_id}')
        return target

    # ------------------------------------------------------------------ I/O-thread API

    def should_write(self, arrival_unix_ns: int) -> bool:
        """Return whether a unit that arrived at `arrival_unix_ns` belongs to the active log.

        A unit is written iff its host arrival time is in `[start_at, stop_at)`. This call also
        performs pending segment transitions (opening files, finalizing at the stop time), so it
        must be called from the I/O thread.
        """
        self._service(arrival_unix_ns, data=True)
        with self._lock:
            active = self._active
            return (
                active is not None
                and active.segment_open
                and not active.segment_ended
                and active.start_at_ns <= arrival_unix_ns
                and (active.stop_at_ns is None or arrival_unix_ns < active.stop_at_ns)
            )

    def on_written(self, nbytes: int, arrival_unix_ns: int) -> None:
        """Record that a unit of `nbytes` that arrived at `arrival_unix_ns` was written.

        The first written unit of a segment defines its actual start and triggers a manifest write.
        """
        with self._lock:
            active = self._require_active()
            active.bytes_written += nbytes
            active.last_written_ns = arrival_unix_ns
            first = active.segment.start_unix_ns is None
            if first:
                active.segment.start_unix_ns = arrival_unix_ns
                logger.info('log %s segment %d: first unit written', active.manifest.log_id, active.segment.index)
        if first:
            self._write_manifest_snapshot(active)

    def tick(self, now_unix_ns: int | None = None) -> None:
        """Perform time-driven transitions: open pending segments, stop at the stop time, refresh the manifest.

        Must be called regularly from the I/O thread, also when no data arrives.
        """
        self._service(self._clock() if now_unix_ns is None else now_unix_ns, data=False)

    def write_failed(self, message: str) -> None:
        """Report a write error (e.g. disk full) on the I/O thread.

        The current segment is closed and marked `ended_cleanly: false`, the session reports a
        fault (health `error`), and nothing more is written. The recording state stays
        `recording` so that a regular stop still finalizes the log.
        """
        with self._lock:
            active = self._require_active()
        logger.error('write failure in log %s: %s', active.manifest.log_id, message)
        self._end_segment(active, clean=False, reason=message)

    def segment_path(self, stem: str, ext: str) -> Path:
        """Return the path `<log_dir>/<stem>.<segment:03d><ext>` and register it in the manifest.

        Example: `segment_path('rover', '.sbf')` → `<log_dir>/rover.000.sbf`.
        """
        with self._lock:
            active = self._require_active()
            name = f'{stem}.{active.segment.index:03d}{ext}'
            if name not in active.segment.files:
                active.segment.files.append(name)
                active.manifest_dirty = True
            return active.log_dir / name

    def close(self) -> None:
        """Finalize the active log like a clean stop (for SIGINT/SIGTERM).

        Call from the I/O thread, or after the I/O thread has exited.
        """
        with self._lock:
            active = self._active
            if active is None or active.finalizing:
                return
            now = self._clock()
            if active.stop_at_ns is None or active.stop_at_ns > now:
                active.stop_at_ns = now
                active.manifest.scheduled_stop_unix_ns = now
        self._finalize(active)

    # ------------------------------------------------------------------ internals

    def _recording_locked(self, now: int) -> Recording:
        active = self._active
        if active is None:
            return Recording()
        if active.stop_requested or active.finalizing:
            state = 'stopping'
        elif active.segment.start_unix_ns is None and now < active.start_at_ns:
            state = 'armed'
        else:
            state = 'recording'
        return Recording(
            state=state,
            log_id=active.manifest.log_id,
            origin=active.manifest.origin,
            scheduled_start_unix_ns=active.start_at_ns,
            actual_start_unix_ns=active.segment.start_unix_ns,
            scheduled_stop_unix_ns=active.stop_at_ns,
            segment_index=active.segment.index,
            bytes_written=active.bytes_written,
        )

    def _require_active(self) -> _ActiveLog:
        if self._active is None:
            raise RuntimeError('no active log')
        return self._active

    def _prepare_manifest(
        self, log_dir: Path, log_id: str, origin: Origin, start_at: int, stop_at: int | None
    ) -> Manifest:
        """Return the manifest for a new segment: a fresh one, or the existing one extended (rejoin)."""
        if log_dir.exists():
            try:
                manifest = read_manifest(log_dir)
            except (OSError, ValueError) as exc:
                logger.warning('log %s: unreadable manifest (%s); starting a new manifest', log_id, exc)
                manifest = self._new_manifest(log_id, origin, start_at, stop_at)
                next_index = self._next_index_from_files(log_dir)
            else:
                next_index = manifest.segments[-1].index + 1 if manifest.segments else 0
                manifest.state = 'recording'
                manifest.scheduled_stop_unix_ns = stop_at
                logger.info('log %s: rejoining as segment %d', log_id, next_index)
        else:
            log_dir.mkdir(parents=True)
            manifest = self._new_manifest(log_id, origin, start_at, stop_at)
            next_index = 0
        manifest.segments.append(Segment(index=next_index))
        return manifest

    def _new_manifest(self, log_id: str, origin: Origin, start_at: int, stop_at: int | None) -> Manifest:
        return Manifest(
            log_id=log_id,
            origin=origin,
            state='recording',
            source=self._source,
            scheduled_start_unix_ns=start_at,
            scheduled_stop_unix_ns=stop_at,
        )

    @staticmethod
    def _next_index_from_files(log_dir: Path) -> int:
        numbers = [int(m.group(1)) for entry in log_dir.iterdir() if (m := _SEGMENT_NUMBER_RE.search(entry.name))]
        return max(numbers) + 1 if numbers else 0

    def _service(self, now: int, *, data: bool) -> None:
        with self._lock:
            active = self._active
            if active is None or active.finalizing:
                return
            need_open = not active.segment_open and not active.segment_ended
        if need_open:
            self._open_segment(active)
        with self._lock:
            grace = 0 if data else self._grace_ns
            due = active.stop_at_ns is not None and now >= active.stop_at_ns + grace
            refresh = active.manifest_dirty or now - active.last_manifest_write_ns >= self._refresh_ns
        if due:
            self._finalize(active)
        elif refresh:
            self._write_manifest_snapshot(active)

    def _open_segment(self, active: _ActiveLog) -> None:
        try:
            self._handler.open_segment(self)
        except OSError as exc:
            logger.exception('log %s: failed to open segment files', active.manifest.log_id)
            with self._lock:
                active.segment_open = True
            self._end_segment(active, clean=False, reason=f'failed to open segment files: {exc}')
            return
        with self._lock:
            active.segment_open = True
            active.manifest_dirty = True

    def _end_segment(self, active: _ActiveLog, *, clean: bool, reason: str | None = None, write: bool = True) -> None:
        """Close the current segment's files, record how it ended and (if `write`) persist the manifest."""
        with self._lock:
            if active.segment_ended:
                return
            active.segment_ended = True
            was_open = active.segment_open
            if not clean:
                self._fault = reason
        if was_open:
            try:
                self._handler.close_segment(self, clean)
            except OSError as exc:
                logger.exception('log %s: failed to close segment files', active.manifest.log_id)
                clean = False
                with self._lock:
                    self._fault = f'failed to close segment files: {exc}'
        with self._lock:
            segment = active.segment
            segment.ended_cleanly = clean
            if segment.start_unix_ns is None:
                segment.end_unix_ns = None
            elif clean and active.stop_at_ns is not None:
                segment.end_unix_ns = max(active.stop_at_ns, segment.start_unix_ns)
            else:
                segment.end_unix_ns = active.last_written_ns
            active.manifest_dirty = True
        if write:
            self._write_manifest_snapshot(active)

    def _finalize(self, active: _ActiveLog) -> None:
        """End the active log: close the segment, write the final manifest, return to idle."""
        with self._lock:
            active.finalizing = True
        self._end_segment(active, clean=True, write=False)
        with self._lock:
            manifest = active.manifest
            complete = len(manifest.segments) == 1 and all(s.ended_cleanly for s in manifest.segments)
            manifest.state = 'complete' if complete else 'interrupted'
        self._write_manifest_snapshot(active)
        with self._lock:
            self._active = None
        logger.info('log %s finalized (%s)', manifest.log_id, manifest.state)

    def _write_manifest_snapshot(self, active: _ActiveLog) -> None:
        with self._lock:
            if not active.segment_ended and active.last_written_ns is not None:
                active.segment.end_unix_ns = active.last_written_ns
            snapshot = active.manifest.model_copy(deep=True)
            active.manifest_dirty = False
            active.last_manifest_write_ns = self._clock()
        try:
            write_manifest(active.log_dir, snapshot)
        except OSError as exc:
            logger.exception('log %s: failed to write manifest', snapshot.log_id)
            with self._lock:
                self._fault = f'failed to write manifest: {exc}'

    def _recover_interrupted(self) -> None:
        """Mark every manifest still in state `recording` as `interrupted` (server crashed while recording)."""
        for log_dir in sorted(self._directory.iterdir()):
            if not (log_dir.is_dir() and is_valid_log_id(log_dir.name) and (log_dir / MANIFEST_NAME).exists()):
                continue
            try:
                manifest = read_manifest(log_dir)
            except (OSError, ValueError) as exc:
                logger.warning('log %s: unreadable manifest (%s)', log_dir.name, exc)
                continue
            if manifest.state != 'recording':
                continue
            manifest.state = 'interrupted'
            if manifest.segments:
                manifest.segments[-1].ended_cleanly = False
            write_manifest(log_dir, manifest)
            logger.warning('log %s was still recording at startup; marked interrupted', log_dir.name)

    def _log_dir_or_404(self, log_id: str) -> Path:
        log_dir = self._directory / log_id
        if not is_valid_log_id(log_id) or not log_dir.is_dir() or log_dir.is_symlink():
            raise ApiError(404, 'log_not_found', f'no log {log_id!r}')
        return log_dir

    def _log_detail(self, log_id: str) -> LogDetail:
        log_dir = self._directory / log_id
        manifest: Manifest | None
        with self._lock:
            active = self._active
            if active is not None and active.manifest.log_id == log_id:
                manifest = active.manifest.model_copy(deep=True)
                if not active.segment_ended and active.last_written_ns is not None:
                    manifest.segments[-1].end_unix_ns = active.last_written_ns
            else:
                manifest = None
        if manifest is None:
            try:
                manifest = read_manifest(log_dir)
            except (OSError, ValueError):
                manifest = None
        files = list_files(log_dir)
        total_bytes = sum(entry.size for entry in files)
        if manifest is None:
            return LogDetail(log_id=log_id, state='interrupted', files=files, total_bytes=total_bytes)
        starts = [s.start_unix_ns for s in manifest.segments if s.start_unix_ns is not None]
        ends = [s.end_unix_ns for s in manifest.segments if s.end_unix_ns is not None]
        return LogDetail(
            log_id=log_id,
            origin=manifest.origin,
            state=manifest.state,
            start_unix_ns=min(starts) if starts else None,
            end_unix_ns=max(ends) if ends else None,
            segments=manifest.segments,
            files=files,
            total_bytes=total_bytes,
            manifest=manifest.model_dump(mode='json'),
        )
