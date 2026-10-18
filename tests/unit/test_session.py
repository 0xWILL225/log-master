"""Unit tests of LogSession: gating, manifest lifecycle, segments, rejoin, faults, file resolution."""

import json
from pathlib import Path

import pytest
from logserver.errors import ApiError
from logserver.ids import is_valid_log_id
from logserver.models import ManifestSource, StartRequest, StopRequest
from logserver.session import LogSession, read_manifest

LOG_ID = '2026-10-17T14-03-55Z_a1b2c3'
OTHER_ID = '2026-10-17T15-00-00Z_000000'
SOURCE = ManifestSource(name='test', source_type='unit', server_version='0', hostname='host')


class Clock:
    """Manually advanced nanosecond clock."""

    def __init__(self, now: int = 1_000_000):
        self.now = now

    def __call__(self) -> int:
        """Return the current time."""
        return self.now


class Handler:
    """Segment handler writing one data file per segment."""

    def __init__(self):
        self.events: list[tuple] = []
        self.file = None

    def open_segment(self, session: LogSession) -> None:
        """Open the segment's data file."""
        path = session.segment_path('data', '.bin')
        self.file = open(path, 'ab')
        self.events.append(('open', path.name))

    def close_segment(self, session: LogSession, clean: bool) -> None:
        """Close the segment's data file."""
        self.file.close()
        self.events.append(('close', clean))


def make_session(tmp_path: Path, clock: Clock | None = None) -> tuple[LogSession, Handler, Clock]:
    """Create a session on `tmp_path/logs` with a manual clock."""
    clock = clock or Clock()
    handler = Handler()
    return LogSession(tmp_path / 'logs', SOURCE, handler, clock=clock, stop_grace_s=0.0), handler, clock


def write(session: LogSession, handler: Handler, arrival: int, data: bytes = b'unit') -> bool:
    """Feed one unit through the gate like a backend I/O loop."""
    if not session.should_write(arrival):
        return False
    handler.file.write(data)
    session.on_written(len(data), arrival)
    return True


def test_gating_boundaries_are_half_open(tmp_path: Path) -> None:
    """Units are written iff start_at <= arrival < stop_at."""
    session, handler, clock = make_session(tmp_path)
    session.start(StartRequest(log_id=LOG_ID, start_at_unix_ns=2_000_000, stop_at_unix_ns=3_000_000))
    assert session.recording().state == 'armed'
    assert not write(session, handler, 1_999_999)
    assert write(session, handler, 2_000_000)
    assert session.recording().state == 'recording'
    assert write(session, handler, 2_999_999)
    assert not write(session, handler, 3_000_000)
    assert session.recording().state == 'idle'
    manifest = read_manifest(tmp_path / 'logs' / LOG_ID)
    assert manifest.state == 'complete'
    assert manifest.segments[0].start_unix_ns == 2_000_000
    assert manifest.segments[0].end_unix_ns == 3_000_000
    assert manifest.segments[0].ended_cleanly
    assert manifest.segments[0].files == ['data.000.bin']
    assert handler.events == [('open', 'data.000.bin'), ('close', True)]


def test_manifest_lifecycle(tmp_path: Path) -> None:
    """The manifest is written at start, at the first unit and at the stop."""
    session, handler, clock = make_session(tmp_path)
    recording = session.start(StartRequest(log_id=LOG_ID))
    assert recording.state == 'recording'
    assert recording.origin == 'log-master'
    log_dir = tmp_path / 'logs' / LOG_ID
    manifest = read_manifest(log_dir)
    assert manifest.state == 'recording'
    assert manifest.segments[0].start_unix_ns is None and manifest.segments[0].end_unix_ns is None

    clock.now += 10
    assert write(session, handler, clock.now)
    assert read_manifest(log_dir).segments[0].start_unix_ns == clock.now
    assert session.recording().actual_start_unix_ns == clock.now
    assert session.recording().bytes_written == 4

    stopping = session.stop(StopRequest(log_id=LOG_ID))
    assert stopping.state == 'stopping'
    session.tick()
    manifest = read_manifest(log_dir)
    assert manifest.state == 'complete'
    assert manifest.scheduled_stop_unix_ns == clock.now
    assert session.recording().state == 'idle'


def test_start_is_idempotent_and_conflicts_are_reported(tmp_path: Path) -> None:
    """Same log ID → 200; different log ID → 409 already_recording with details."""
    session, _, _ = make_session(tmp_path)
    first = session.start(StartRequest(log_id=LOG_ID))
    assert session.start(StartRequest(log_id=LOG_ID)) == first
    with pytest.raises(ApiError) as excinfo:
        session.start(StartRequest(log_id=OTHER_ID))
    assert excinfo.value.status_code == 409
    assert excinfo.value.code == 'already_recording'
    assert excinfo.value.details == {'log_id': LOG_ID, 'origin': 'log-master'}
    with pytest.raises(ApiError) as excinfo:
        session.start(StartRequest())
    assert excinfo.value.code == 'already_recording'


def test_stop_semantics(tmp_path: Path) -> None:
    """Stop is idempotent when idle and refuses a mismatched log ID."""
    session, _, _ = make_session(tmp_path)
    assert session.stop(StopRequest()).state == 'idle'
    session.start(StartRequest(log_id=LOG_ID))
    with pytest.raises(ApiError) as excinfo:
        session.stop(StopRequest(log_id=OTHER_ID))
    assert excinfo.value.code == 'log_id_mismatch'


def test_stop_never_extends_a_timed_log(tmp_path: Path) -> None:
    """A later stop time does not override an earlier one."""
    session, _, clock = make_session(tmp_path)
    session.start(StartRequest(log_id=LOG_ID, stop_at_unix_ns=clock.now + 100))
    assert session.stop(StopRequest(stop_at_unix_ns=clock.now + 500)).scheduled_stop_unix_ns == clock.now + 100
    assert session.stop(StopRequest(stop_at_unix_ns=clock.now + 50)).scheduled_stop_unix_ns == clock.now + 50


def test_standalone_start_generates_an_id(tmp_path: Path) -> None:
    """Without log_id the server generates an ID and marks the log standalone."""
    session, _, _ = make_session(tmp_path, Clock(1_792_245_837_000_000_000))
    recording = session.start(StartRequest())
    assert recording.origin == 'standalone'
    assert is_valid_log_id(recording.log_id)
    assert recording.log_id.startswith('2026-10-17T')


def test_invalid_times_are_rejected(tmp_path: Path) -> None:
    """stop_at must be after start_at."""
    session, _, _ = make_session(tmp_path)
    with pytest.raises(ApiError) as excinfo:
        session.start(StartRequest(log_id=LOG_ID, start_at_unix_ns=10, stop_at_unix_ns=10))
    assert excinfo.value.status_code == 400


def test_timed_stop_without_data(tmp_path: Path) -> None:
    """tick() finalizes at the stop time even when no data arrives."""
    session, handler, clock = make_session(tmp_path)
    session.start(StartRequest(log_id=LOG_ID, stop_at_unix_ns=clock.now + 1000))
    session.tick()
    assert handler.events == [('open', 'data.000.bin')]
    clock.now += 999
    session.tick()
    assert session.active_log_id == LOG_ID
    clock.now += 1
    session.tick()
    assert session.active_log_id is None
    manifest = read_manifest(tmp_path / 'logs' / LOG_ID)
    assert manifest.state == 'complete'
    assert manifest.segments[0].start_unix_ns is None


def test_crash_recovery_and_rejoin(tmp_path: Path) -> None:
    """A crashed log is swept to interrupted; a restart for the same ID opens segment 1."""
    session, handler, clock = make_session(tmp_path)
    session.start(StartRequest(log_id=LOG_ID))
    write(session, handler, clock.now)
    handler.file.close()

    clock.now += 5_000
    restarted, handler2, _ = make_session(tmp_path, clock)
    swept = read_manifest(tmp_path / 'logs' / LOG_ID)
    assert swept.state == 'interrupted'
    assert not swept.segments[0].ended_cleanly

    recording = restarted.start(StartRequest(log_id=LOG_ID))
    assert recording.segment_index == 1
    assert write(restarted, handler2, clock.now)
    assert restarted.recording().segment_index == 1
    restarted.stop(StopRequest())
    restarted.tick()
    manifest = read_manifest(tmp_path / 'logs' / LOG_ID)
    assert [s.index for s in manifest.segments] == [0, 1]
    assert manifest.segments[1].files == ['data.001.bin']
    assert manifest.state == 'interrupted'
    assert handler2.events == [('open', 'data.001.bin'), ('close', True)]


def test_rejoin_with_unreadable_manifest_continues_numbering(tmp_path: Path) -> None:
    """Segment numbers continue after existing files when the manifest is corrupt."""
    log_dir = tmp_path / 'logs' / LOG_ID
    log_dir.mkdir(parents=True)
    (log_dir / 'manifest.json').write_text('{not json')
    (log_dir / 'data.000.bin').write_bytes(b'x')
    session, _, _ = make_session(tmp_path)
    assert session.start(StartRequest(log_id=LOG_ID)).segment_index == 1


def test_write_failure_ends_segment_uncleanly(tmp_path: Path) -> None:
    """A write error closes the segment, reports a fault and keeps the state recording until stopped."""
    session, handler, clock = make_session(tmp_path)
    session.start(StartRequest(log_id=LOG_ID))
    write(session, handler, clock.now)
    session.write_failed('No space left on device')
    assert session.fault == 'No space left on device'
    assert session.recording().state == 'recording'
    assert not session.should_write(clock.now + 1)
    assert handler.events[-1] == ('close', False)
    session.stop(StopRequest())
    session.tick()
    manifest = read_manifest(tmp_path / 'logs' / LOG_ID)
    assert manifest.state == 'interrupted'
    assert not manifest.segments[0].ended_cleanly
    assert manifest.segments[0].end_unix_ns == clock.now


def test_close_finalizes_like_a_clean_stop(tmp_path: Path) -> None:
    """close() (SIGTERM path) finalizes the manifest as complete."""
    session, handler, clock = make_session(tmp_path)
    session.start(StartRequest(log_id=LOG_ID))
    write(session, handler, clock.now)
    session.close()
    assert read_manifest(tmp_path / 'logs' / LOG_ID).state == 'complete'
    assert session.active_log_id is None


def test_listing(tmp_path: Path) -> None:
    """Listing is newest first, ignores non-log directories and reports corrupt manifests as interrupted."""
    session, handler, clock = make_session(tmp_path)
    session.start(StartRequest(log_id=LOG_ID))
    write(session, handler, clock.now)
    session.close()
    logs = tmp_path / 'logs'
    (logs / 'not-a-log').mkdir()
    (logs / OTHER_ID).mkdir()
    (logs / OTHER_ID / 'manifest.json').write_text('garbage')
    entries = session.list_logs()
    assert [e.log_id for e in entries] == [OTHER_ID, LOG_ID]
    assert entries[0].state == 'interrupted' and entries[0].origin is None
    assert entries[1].state == 'complete'
    assert {f.path for f in entries[1].files} == {'data.000.bin', 'manifest.json'}
    assert entries[1].total_bytes == sum(f.size for f in entries[1].files)
    detail = session.get_log(LOG_ID)
    assert detail.manifest['log_id'] == LOG_ID
    with pytest.raises(ApiError):
        session.get_log('2026-01-01T00-00-00Z_ffffff')


def test_listing_of_active_log_uses_live_state(tmp_path: Path) -> None:
    """The active log is listed as recording with its latest end time."""
    session, handler, clock = make_session(tmp_path)
    session.start(StartRequest(log_id=LOG_ID))
    write(session, handler, clock.now)
    write(session, handler, clock.now + 50)
    entry = session.list_logs()[0]
    assert entry.state == 'recording'
    assert entry.end_unix_ns == clock.now + 50


def test_resolve_file_rejects_escapes(tmp_path: Path) -> None:
    """Paths escaping the log directory (.., absolute, symlinks out) are 404."""
    session, handler, clock = make_session(tmp_path)
    session.start(StartRequest(log_id=LOG_ID))
    session.close()
    log_dir = tmp_path / 'logs' / LOG_ID
    secret = tmp_path / 'secret.txt'
    secret.write_text('secret')
    (log_dir / 'link').symlink_to(secret)
    assert session.resolve_file(LOG_ID, 'manifest.json') == (log_dir / 'manifest.json').resolve()
    for bad in ('../../secret.txt', str(secret), 'link', 'missing', '.', ''):
        with pytest.raises(ApiError) as excinfo:
            session.resolve_file(LOG_ID, bad)
        assert excinfo.value.status_code == 404
    with pytest.raises(ApiError):
        session.resolve_file('../logs', 'manifest.json')


def test_manifest_is_valid_json_with_trailing_newline(tmp_path: Path) -> None:
    """Manifests are human-readable JSON."""
    session, _, _ = make_session(tmp_path)
    session.start(StartRequest(log_id=LOG_ID))
    text = (tmp_path / 'logs' / LOG_ID / 'manifest.json').read_text()
    assert text.endswith('\n')
    assert json.loads(text)['manifest_version'] == 1
