"""Fixtures for integration tests: logserver-fake processes and a log-master daemon on localhost."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

# ROS installations put foreign packages on PYTHONPATH; keep the child processes clean.
CHILD_ENV = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}


def free_port() -> int:
    """Return a currently unused TCP port on localhost."""
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def wait_until(predicate: Callable[[], Any], timeout: float = 10.0, message: str = 'condition') -> Any:
    """Poll `predicate` until it returns a truthy value and return that value."""
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if result:
            return result
        if time.monotonic() > deadline:
            raise AssertionError(f'timed out waiting for {message}')
        time.sleep(0.05)


class Process:
    """A child process whose output goes to a log file."""

    def __init__(self, args: list[str], log_path: Path):
        self.args = args
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        """Start the process."""
        log = open(self.log_path, 'ab')
        self.proc = subprocess.Popen(self.args, stdout=log, stderr=subprocess.STDOUT, env=CHILD_ENV)
        log.close()

    def kill(self) -> None:
        """SIGKILL the process (simulates a crash)."""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)

    def terminate(self) -> None:
        """SIGTERM the process and wait for it to exit."""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            self.proc.wait(10)

    def output(self) -> str:
        """Return everything the process logged."""
        return self.log_path.read_text(errors='replace')


class FakeServer(Process):
    """A `logserver-fake` instance."""

    def __init__(self, name: str, tmp_path: Path, extra_args: tuple[str, ...] = ()):
        self.name = name
        self.port = free_port()
        self.dir = tmp_path / name
        self.url = f'http://127.0.0.1:{self.port}'
        args = [
            sys.executable,
            '-m',
            'logserver.fake.main',
            '--name',
            name,
            '--host',
            '127.0.0.1',
            '--port',
            str(self.port),
            '--dir',
            str(self.dir),
            '--rate',
            '20kB/s',
            '--units-per-s',
            '100',
            '--log-level',
            'warning',
            *extra_args,
        ]
        super().__init__(args, tmp_path / f'{name}.log')

    def start(self) -> None:
        """Start and wait until the HTTP API answers."""
        super().start()
        wait_until(self.ping, message=f'{self.name} to start')

    def ping(self) -> bool:
        """Return whether the server answers `/v1/info`."""
        try:
            return httpx.get(f'{self.url}/v1/info', timeout=0.5).status_code == 200
        except httpx.HTTPError:
            return False

    def recording(self) -> dict[str, Any]:
        """Return the server's `recording` status."""
        return httpx.get(f'{self.url}/v1/status', timeout=2).json()['recording']

    def post(self, path: str, body: Any = None) -> httpx.Response:
        """POST to the server's `/v1` API."""
        return httpx.post(f'{self.url}/v1{path}', json=body, timeout=2)

    def manifest(self, log_id: str) -> dict[str, Any]:
        """Read a manifest from disk."""
        return json.loads((self.dir / log_id / 'manifest.json').read_text())


class LogMaster(Process):
    """A `log-master serve` daemon with fast polling, configured for the given fakes."""

    def __init__(self, tmp_path: Path, sources: list[FakeServer], settings: dict[str, Any] | None = None):
        self.port = free_port()
        self.base = f'http://127.0.0.1:{self.port}'
        self.url = f'{self.base}/api/v1'
        self.config_path = tmp_path / 'log-master.yaml'
        self.state_path = tmp_path / 'state.json'
        config = {
            'http': {'bind': f'127.0.0.1:{self.port}'},
            'poll': {'interval_s': 0.2, 'timeout_s': 0.3, 'offline_after_failures': 2},
            'listing': {'timeout_s': 3.0},
            'recording': {'start_lead_s': 0.5, 'stop_lead_s': 0.3, 'late_start_warn_ms': 500},
            'state_file': str(self.state_path),
            'sources': [{'name': s.name, 'url': s.url, 'enabled_by_default': True} for s in sources],
            **(settings or {}),
        }
        self.config_path.write_text(json.dumps(config, indent=2))
        args = [sys.executable, '-m', 'log_master.cli.main', 'serve', '--config', str(self.config_path)]
        super().__init__(args, tmp_path / 'log-master.log')
        self._request_counter = 0

    def start(self) -> None:
        """Start and wait until the API answers."""
        super().start()
        wait_until(self._ping, message='log-master to start')

    def _ping(self) -> bool:
        try:
            return httpx.get(f'{self.url}/state', timeout=0.5).status_code == 200
        except httpx.HTTPError:
            return False

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        """GET from the REST API."""
        return httpx.get(f'{self.url}{path}', timeout=10, **kwargs)

    def post(self, path: str, body: Any = None) -> httpx.Response:
        """POST to the REST API."""
        return httpx.post(f'{self.url}{path}', json=body, timeout=10)

    def start_log(self, **body: Any) -> httpx.Response:
        """POST /recording/start with a fresh request ID."""
        self._request_counter += 1
        return self.post('/recording/start', {'request_id': f'test-{self._request_counter}-{time.time_ns()}', **body})

    def state(self) -> dict[str, Any]:
        """GET /state."""
        return self.get('/state').json()

    def participant(self, name: str) -> dict[str, Any] | None:
        """Return the participant entry of `name` in the active log."""
        active = self.state()['active_log']
        if active is None:
            return None
        return next((p for p in active['participants'] if p['name'] == name), None)

    def wait_participant(self, name: str, status: str, timeout: float = 10.0) -> dict[str, Any]:
        """Wait until participant `name` has `status`."""
        return wait_until(
            lambda: (p := self.participant(name)) is not None and p['status'] == status and p,
            timeout,
            f'{name} to be {status}',
        )

    def source(self, name: str) -> dict[str, Any]:
        """Return the source summary of `name` from /state."""
        return next(s for s in self.state()['sources'] if s['name'] == name)

    def cli(self, *args: str) -> subprocess.CompletedProcess:
        """Run the log-master CLI against this daemon."""
        return subprocess.run(
            [sys.executable, '-m', 'log_master.cli.main', '--url', self.base, *args],
            capture_output=True,
            text=True,
            env={**CHILD_ENV, 'COLUMNS': '200'},
            timeout=60,
        )


@pytest.fixture
def processes() -> Iterator[list[Process]]:
    """Registry of started processes; all are killed after the test."""
    started: list[Process] = []
    yield started
    for process in started:
        process.kill()


@pytest.fixture
def make_fake(tmp_path: Path, processes: list[Process]) -> Callable[..., FakeServer]:
    """Factory starting a `logserver-fake` (pass `start=False` to only create it)."""

    def make(name: str, *extra_args: str, start: bool = True) -> FakeServer:
        fake = FakeServer(name, tmp_path, tuple(extra_args))
        processes.append(fake)
        if start:
            fake.start()
        return fake

    return make


@pytest.fixture
def make_log_master(tmp_path: Path, processes: list[Process]) -> Callable[..., LogMaster]:
    """Factory starting a log-master daemon for the given fakes and waiting until they are online."""

    def make(sources: list[FakeServer], *, wait_online: bool = True, **settings: Any) -> LogMaster:
        log_master = LogMaster(tmp_path, sources, settings)
        processes.append(log_master)
        log_master.start()
        if wait_online:
            running = [s.name for s in sources if s.proc is not None and s.proc.poll() is None]
            wait_until(lambda: all(log_master.source(n)['online'] for n in running), message='sources to come online')
        return log_master

    return make
