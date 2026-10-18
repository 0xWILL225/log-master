"""Backend of `logserver-fake`: writes timestamped pseudo-data and offers fault injection.

The fake is a complete logging server built on the `logserver` package. It is meant for
developing the log-master UI and for integration tests, so besides pseudo-data it provides
a sample config schema (read-only, secret and restart-required fields) and sample actions,
including faults: `crash` (exit without finalizing), `go-offline` (stop answering HTTP for a
while), `disk-full` (simulated write error) and `set-health`.
"""

import errno
import logging
import math
import os
import socket
import threading
import time
from typing import Any, BinaryIO, Literal, TextIO

from pydantic import BaseModel, ConfigDict

from logserver import __version__
from logserver.config import ConfigStore, config_field
from logserver.errors import ApiError
from logserver.models import ActionResult, ActionSpec, Detail, Health, HealthLevel, Info, ManifestSource
from logserver.runner import StopFlag
from logserver.session import LogSession

logger = logging.getLogger(__name__)

SOURCE_TYPE = 'fake'
_PATTERN = bytes(range(256))


class FakeConfig(BaseModel):
    """Configuration of `logserver-fake` (also serves as a sample config schema)."""

    model_config = ConfigDict(extra='forbid', title='Fake logging server')

    name: str = config_field('fake', read_only=True, title='Name')
    log_directory: str = config_field('/tmp/logserver-fake', read_only=True, title='Log directory')
    bind_address: str = config_field('0.0.0.0:8101', read_only=True, title='Bind address')
    rate_bytes_per_s: int = config_field(200_000, ge=1, title='Data rate (B/s)')
    units_per_s: int = config_field(50, ge=1, le=1000, restart_required=True, title='Units per second')
    file_stem: str = config_field(
        'fake', pattern=r'^[A-Za-z0-9_-]+$', title='File stem', description='Applies from the next segment.'
    )
    mode: Literal['text', 'binary'] = config_field('text', title='Data format')
    slow_start_ms: int = config_field(
        0, ge=0, title='Start latency (ms)', description='Delay of the first written unit after the start time.'
    )
    health_override: HealthLevel | None = config_field(None, title='Health override')
    api_token: str = config_field('', write_only=True, title='API token', description='Sample secret field.')


class FakeBackend:
    """Logging-server backend generating pseudo-data at a configurable rate."""

    def __init__(self, config: ConfigStore[FakeConfig]):
        self.config = config
        self.config.on_change = self._on_config_change
        values = config.values
        self.info = Info(
            name=values.name,
            source_type=SOURCE_TYPE,
            server_version=__version__,
            hostname=socket.gethostname(),
            log_directory=values.log_directory,
        )
        self._lock = threading.Lock()
        self._units_per_s = values.units_per_s
        self._rate = values.rate_bytes_per_s
        self._mode = values.mode
        self._slow_start_ns = values.slow_start_ms * 1_000_000
        self._health = Health(level=values.health_override) if values.health_override else Health()
        self._units_generated = 0
        self._started_mono = time.monotonic()
        self._offline_until = 0.0
        self._disk_full = False
        self._survey: dict[str, Any] | None = None
        self._data_file: BinaryIO | None = None
        self._timing_file: TextIO | None = None
        self.session = LogSession(
            values.log_directory,
            ManifestSource(
                name=values.name,
                source_type=SOURCE_TYPE,
                server_version=__version__,
                hostname=self.info.hostname,
            ),
            self,
        )

    # ------------------------------------------------------------------ segment handler

    def open_segment(self, session: LogSession) -> None:
        """Open the data file and its timing sidecar for a new segment."""
        stem = self.config.values.file_stem
        ext = '.txt' if self._mode == 'text' else '.bin'
        self._data_file = open(session.segment_path(stem, ext), 'ab')
        self._timing_file = open(session.segment_path(stem, '.timing.csv'), 'a')
        if self._timing_file.tell() == 0:
            self._timing_file.write('seq,arrival_unix_ns,bytes\n')
        with self._lock:
            self._disk_full = False

    def close_segment(self, session: LogSession, clean: bool) -> None:
        """Close the segment's files."""
        for file in (self._data_file, self._timing_file):
            if file is not None:
                file.close()
        self._data_file = None
        self._timing_file = None

    # ------------------------------------------------------------------ I/O loop

    def run_io_loop(self, stop: StopFlag) -> None:
        """Generate units until a stop is requested; this is the server's I/O thread."""
        period = 1.0 / self._units_per_s
        deadline = time.monotonic()
        seq = 0
        while not stop.requested:
            self.session.tick()
            arrival = time.time_ns()
            unit = self._make_unit(seq, arrival)
            with self._lock:
                self._units_generated += 1
            if self.session.should_write(arrival) and not self._in_slow_start(arrival):
                self._write_unit(seq, unit, arrival)
            seq += 1
            deadline += period
            delay = deadline - time.monotonic()
            if delay < -1.0:
                deadline = time.monotonic()
            while (remaining := deadline - time.monotonic()) > 0 and not stop.requested:
                time.sleep(min(remaining, 0.1))

    def _make_unit(self, seq: int, arrival: int) -> bytes:
        with self._lock:
            size = max(32, self._rate // self._units_per_s)
            mode = self._mode
        if mode == 'text':
            head = f'{seq},{arrival},'.encode()
            return head + b'x' * max(0, size - len(head) - 1) + b'\n'
        head = seq.to_bytes(8, 'little') + arrival.to_bytes(8, 'little')
        body_size = max(0, size - len(head))
        return head + (_PATTERN * (body_size // len(_PATTERN) + 1))[:body_size]

    def _in_slow_start(self, arrival: int) -> bool:
        if self._slow_start_ns == 0:
            return False
        recording = self.session.recording()
        return (
            recording.actual_start_unix_ns is None
            and recording.scheduled_start_unix_ns is not None
            and arrival < recording.scheduled_start_unix_ns + self._slow_start_ns
        )

    def _write_unit(self, seq: int, unit: bytes, arrival: int) -> None:
        assert self._data_file is not None and self._timing_file is not None
        try:
            with self._lock:
                disk_full = self._disk_full
            if disk_full:
                raise OSError(errno.ENOSPC, 'No space left on device (simulated)')
            self._data_file.write(unit)
            self._timing_file.write(f'{seq},{arrival},{len(unit)}\n')
            self._data_file.flush()
            self._timing_file.flush()
        except OSError as exc:
            self.session.write_failed(str(exc))
        else:
            self.session.on_written(len(unit), arrival)

    # ------------------------------------------------------------------ status

    def health(self) -> Health:
        """Return the (possibly overridden) health."""
        with self._lock:
            return self._health

    def details(self) -> list[Detail]:
        """Return pseudo device status rows."""
        with self._lock:
            units = self._units_generated
            rate = self._rate
            survey = self._survey
        rows = [
            Detail(key='fix_type', label='Fix', value='RTK-FIXED', level='ok'),
            Detail(key='units', label='Units generated', value=units),
            Detail(key='rate', label='Data rate', value=round(rate / 1000, 1), unit='kB/s'),
            Detail(key='uptime', label='Uptime', value=int(time.monotonic() - self._started_mono), unit='s'),
        ]
        if survey is not None:
            rows.append(self._survey_detail(survey))
        return rows

    def offline_remaining(self) -> float:
        """Seconds left of a simulated `go-offline` period."""
        with self._lock:
            return max(0.0, self._offline_until - time.monotonic())

    # ------------------------------------------------------------------ actions

    def actions(self) -> list[ActionSpec]:
        """Return the sample and fault-injection actions."""
        recording = self.session.active_log_id is not None
        return [
            ActionSpec(
                name='survey-in',
                label='Start survey-in',
                description='Simulated survey-in; progress is reported in the status details.',
                params_schema={
                    'type': 'object',
                    'properties': {
                        'seconds': {'type': 'integer', 'minimum': 1, 'default': 60, 'title': 'Min duration (s)'},
                        'accuracy_mm': {
                            'type': 'integer',
                            'minimum': 1,
                            'default': 2000,
                            'title': 'Accuracy limit (mm)',
                        },
                        'immediate': {'type': 'boolean', 'default': False, 'title': 'Fix current position now'},
                    },
                },
            ),
            ActionSpec(
                name='set-health',
                label='Set health',
                description='Override the reported health level.',
                params_schema={
                    'type': 'object',
                    'required': ['level'],
                    'properties': {
                        'level': {'type': 'string', 'enum': ['ok', 'warn', 'error'], 'title': 'Level'},
                        'message': {'type': 'string', 'title': 'Message'},
                    },
                },
            ),
            ActionSpec(
                name='go-offline',
                label='Go offline',
                description='Stop answering HTTP requests for a while (recording continues).',
                params_schema={
                    'type': 'object',
                    'properties': {
                        'seconds': {
                            'type': 'number',
                            'minimum': 0.1,
                            'maximum': 600,
                            'default': 5,
                            'title': 'Duration (s)',
                        }
                    },
                },
            ),
            ActionSpec(
                name='disk-full',
                label='Simulate disk full',
                description='Make the next write fail with ENOSPC.',
                available=recording,
                unavailable_reason=None if recording else 'only while recording',
            ),
            ActionSpec(
                name='crash',
                label='Crash',
                description='Exit immediately without finalizing the manifest.',
                confirm='This kills the fake server without finalizing its log. Continue?',
            ),
        ]

    def run_action(self, name: str, params: dict[str, Any]) -> ActionResult:
        """Run one of the actions listed by :meth:`actions`."""
        if name == 'survey-in':
            with self._lock:
                self._survey = {
                    'started': time.monotonic(),
                    'seconds': params.get('seconds', 60),
                    'accuracy_m': params.get('accuracy_mm', 2000) / 1000,
                    'immediate': params.get('immediate', False),
                }
            return ActionResult(message='survey-in started', data={'params': params})
        if name == 'set-health':
            with self._lock:
                self._health = Health(level=params['level'], message=params.get('message') or None)
            return ActionResult(message=f'health set to {params["level"]}')
        if name == 'go-offline':
            seconds = float(params.get('seconds', 5))
            with self._lock:
                self._offline_until = time.monotonic() + seconds
            return ActionResult(message=f'going offline for {seconds:g} s')
        if name == 'disk-full':
            with self._lock:
                self._disk_full = True
            return ActionResult(message='next write will fail with ENOSPC')
        if name == 'crash':
            logger.warning('crash requested; exiting without finalizing')
            threading.Timer(0.2, os._exit, args=(1,)).start()
            return ActionResult(message='crashing')
        raise ApiError(404, 'action_not_found', f'no action {name!r}')

    @staticmethod
    def _survey_detail(survey: dict[str, Any]) -> Detail:
        elapsed = time.monotonic() - survey['started']
        accuracy = 0.0 if survey['immediate'] else 3.0 * math.exp(-elapsed / 20)
        if survey['immediate'] or (elapsed >= survey['seconds'] and accuracy <= survey['accuracy_m']):
            return Detail(key='survey_in', label='Survey-in', value='complete', level='ok')
        return Detail(
            key='survey_in', label='Survey-in', value=f'in progress, {int(elapsed)} s, {accuracy:.2f} m', level='warn'
        )

    # ------------------------------------------------------------------ config

    def _on_config_change(self, old: FakeConfig, new: FakeConfig, changed: set[str]) -> None:
        with self._lock:
            self._rate = new.rate_bytes_per_s
            self._mode = new.mode
            self._slow_start_ns = new.slow_start_ms * 1_000_000
            if 'health_override' in changed:
                self._health = Health(level=new.health_override) if new.health_override else Health()
