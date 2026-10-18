# logserver

Reference implementation of the log-master **Logging Server API** (`spec/logserver.openapi.yaml`,
`SPEC.md` §4–§5) for Python logging servers, and `logserver-fake`, a fake server for development
and tests.

A server implements a small backend and gets every `/v1` endpoint, log directories and
manifests, segment numbering, crash recovery and rejoin, start/stop gating, Range file
serving, and config persistence.

## Writing a server

```python
import threading

from pydantic import BaseModel
from logserver.config import ConfigStore, config_field
from logserver.models import ActionResult, ActionSpec, Detail, Health, Info, ManifestSource
from logserver.router import create_app
from logserver.runner import StopFlag, run_in_thread
from logserver.session import LogSession


class Config(BaseModel):
    log_directory: str = config_field('/var/log/rover', read_only=True)
    bind_address: str = config_field('0.0.0.0:8001', read_only=True)
    serial_port: str = config_field('/dev/tty_mosaic-h', restart_required=True)


class Backend:
    def __init__(self) -> None:
        self.config = ConfigStore(Config, '/etc/rover/config.yaml')
        self.info = Info(
            name='rover',
            source_type='septentrio-mosaic-h',
            server_version='0.3.0',
            hostname='robot-pc',
            log_directory=self.config.values.log_directory,
        )
        self.session = LogSession(
            self.config.values.log_directory,
            ManifestSource(
                name='rover', source_type='septentrio-mosaic-h', server_version='0.3.0', hostname='robot-pc'
            ),
            self,
        )
        self.lock = threading.Lock()  # guards state shared with the HTTP threads
        self.file = None

    # Segment callbacks: called on the I/O thread from session.tick()/should_write().
    def open_segment(self, session: LogSession) -> None:
        self.file = open(session.segment_path('rover', '.sbf'), 'ab')

    def close_segment(self, session: LogSession, clean: bool) -> None:
        self.file.close()

    # Called from HTTP threads: keep them cheap and thread-safe.
    def health(self) -> Health: ...
    def details(self) -> list[Detail]: ...
    def actions(self) -> list[ActionSpec]: ...
    def run_action(self, name: str, params: dict) -> ActionResult: ...


backend = Backend()
stop = StopFlag().install()  # SIGINT/SIGTERM only set stop.requested
server = run_in_thread(create_app(backend), host='0.0.0.0', port=8001)
while not stop.requested:  # the existing serial I/O loop
    backend.session.tick()  # timed stops, manifest refreshes
    unit, arrival_ns = read_frame(timeout=0.1)  # host CLOCK_REALTIME arrival time
    if unit and backend.session.should_write(arrival_ns):
        try:
            backend.file.write(unit)  # whole units only
        except OSError as exc:
            backend.session.write_failed(str(exc))  # disk full: segment ends uncleanly
        else:
            backend.session.on_written(len(unit), arrival_ns)
backend.session.close()  # finalize the manifest like a clean stop
server.stop()
```

## Threading rules

- HTTP handlers run in uvicorn's thread pool and only change the session's *intent*
  (`start`/`stop`) or read snapshots. Backend `health`, `details`, `actions` and `run_action`
  must be thread-safe.
- All file opening/closing happens on the I/O thread inside `tick()`, `should_write()`,
  `write_failed()` and `close()`, so the backend's files never race with its writes.
- Hold the backend's lock only to copy or update small pieces of state, never during disk
  or serial I/O, so HTTP traffic cannot delay the I/O loop.
- Signal handlers must not take locks, not even `threading.Event.set()`. Python can run a
  second handler nested inside the first, and the nested call then waits forever for the lock
  the outer one holds. `StopFlag` only sets an attribute; poll it in the I/O loop and keep the
  loop's blocking calls short (the example's 0.1 s read timeout bounds the shutdown delay).

## logserver-fake

```bash
logserver-fake --name fake1 --port 8101 --dir /tmp/fake1 --rate 200kB/s [--slow-start-ms 800] [--health warn]
```

Writes timestamped pseudo-data (`fake.NNN.txt` plus a `fake.NNN.timing.csv` sidecar) and offers
a sample config schema and actions: `survey-in` (progress in the status details), `set-health`,
`go-offline` (stops answering HTTP for N seconds), `disk-full` (next write fails with ENOSPC) and
`crash` (exits without finalizing).
