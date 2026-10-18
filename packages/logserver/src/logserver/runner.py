"""Run a uvicorn server in a background thread beside a server's own I/O loop.

Existing logging servers (rover, base) have a single-threaded main loop polling a serial
device. :func:`run_in_thread` lets them add the HTTP API without restructuring that loop.

Locking pattern
---------------
Use a single `threading.Lock` for the backend's shared state (e.g. the latest device status
shown in `details`). Both the HTTP handlers and the I/O loop hold it only to copy or update
small pieces of state, never during disk or serial I/O, so the I/O loop's latency is not
affected by HTTP traffic. :class:`~logserver.session.LogSession` already has its own lock
following the same rule.

Shutdown
--------
Use :class:`StopFlag` for SIGINT/SIGTERM. Signal handlers must not take locks (not even
`threading.Event.set()`): Python can run a second handler nested inside the first one, and
the nested call then blocks forever on the lock the outer call holds.
"""

import signal
import threading
import time
from types import FrameType

import uvicorn
from fastapi import FastAPI


class StopFlag:
    """Shutdown request set by SIGINT/SIGTERM; the I/O loop polls :attr:`requested`.

    The handler only assigns an attribute, so it is safe however signals arrive.
    """

    def __init__(self) -> None:
        self.requested = False

    def install(self) -> 'StopFlag':
        """Install the handler for SIGINT and SIGTERM (call from the main thread); returns `self`."""
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, self._handle)
        return self

    def _handle(self, _signum: int, _frame: FrameType | None) -> None:
        self.requested = True


class ServerThread:
    """Handle of a uvicorn server running in a daemon thread."""

    def __init__(self, server: uvicorn.Server, thread: threading.Thread):
        self.server = server
        self.thread = thread

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the server to shut down and wait up to `timeout` seconds for the thread to exit."""
        self.server.should_exit = True
        self.thread.join(timeout)


def run_in_thread(
    app: FastAPI, host: str = '0.0.0.0', port: int = 8000, *, log_level: str = 'warning', startup_timeout: float = 10.0
) -> ServerThread:
    """Start `app` with uvicorn in a daemon thread and wait until it accepts connections.

    Raises:
        RuntimeError: If the server does not start within `startup_timeout` seconds (e.g. the
            port is already in use).
    """
    config = uvicorn.Config(app, host=host, port=port, log_level=log_level)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name='logserver-http', daemon=True)
    thread.start()
    deadline = time.monotonic() + startup_timeout
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError(f'HTTP server failed to start on {host}:{port}')
        if time.monotonic() > deadline:
            server.should_exit = True
            raise RuntimeError(f'HTTP server did not start on {host}:{port} within {startup_timeout} s')
        time.sleep(0.01)
    return ServerThread(server, thread)
