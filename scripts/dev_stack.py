"""Run a local development stack: N `logserver-fake` instances plus a log-master daemon.

Usage::

    uv run python scripts/dev_stack.py                 # fakes on 8101.., log-master on 8080
    uv run python scripts/dev_stack.py --fakes 3 --port 18080 --dir /tmp/lm-dev --fresh

Everything stops on Ctrl-C / SIGTERM. Used by `make dev` and by the Playwright smoke tests.
"""

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

SHUTDOWN_TIMEOUT_S = 5.0
CHILD_ENV = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}


def port_in_use(port: int) -> bool:
    """Return whether something is already listening on 127.0.0.1:`port`."""
    with socket.socket() as sock:
        return sock.connect_ex(('127.0.0.1', port)) == 0


def stop_children(children: list[subprocess.Popen]) -> None:
    """SIGTERM all children, then SIGKILL whatever is still running after SHUTDOWN_TIMEOUT_S."""
    for child in reversed(children):
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)
    deadline = time.monotonic() + SHUTDOWN_TIMEOUT_S
    while any(child.poll() is None for child in children) and time.monotonic() < deadline:
        time.sleep(0.05)
    for child in children:
        if child.poll() is None:
            sys.stderr.write(f'{" ".join(child.args[2:5])} did not stop within {SHUTDOWN_TIMEOUT_S:g} s; killing it\n')
            child.kill()
            child.wait()


def _answers(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            return response.status == 200
    except OSError:
        return False


def wait_http(url: str, timeout: float = 20.0) -> None:
    """Wait until `url` answers with HTTP 200.

    Raises:
        RuntimeError: On timeout.
    """
    deadline = time.monotonic() + timeout
    while not _answers(url):
        if time.monotonic() > deadline:
            raise RuntimeError(f'{url} did not come up within {timeout} s')
        time.sleep(0.1)


def main() -> None:
    """Start the stack and wait for a signal."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--fakes', type=int, default=2, help='number of fake servers (default: %(default)s)')
    parser.add_argument('--fake-port', type=int, default=8101, help='port of the first fake (default: %(default)s)')
    parser.add_argument('--port', type=int, default=8080, help='log-master port (default: %(default)s)')
    parser.add_argument('--dir', type=Path, help='working directory (default: a new temporary directory)')
    parser.add_argument('--fresh', action='store_true', help='delete the working directory first')
    parser.add_argument('--poll-interval', type=float, default=1.0, help='log-master poll interval in seconds')
    args = parser.parse_args()

    workdir = args.dir or Path(tempfile.mkdtemp(prefix='log-master-dev-'))
    if args.fresh and workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    names = [f'fake{i + 1}' for i in range(args.fakes)]
    ports = [args.fake_port + i for i in range(args.fakes)]
    config = {
        'http': {'bind': f'127.0.0.1:{args.port}'},
        'poll': {'interval_s': args.poll_interval, 'timeout_s': min(0.8, args.poll_interval * 0.8)},
        'state_file': str(workdir / 'state.json'),
        'sources': [{'name': n, 'url': f'http://127.0.0.1:{p}'} for n, p in zip(names, ports, strict=True)],
    }
    config_path = workdir / 'log-master.yaml'
    if not config_path.exists():
        config_path.write_text(json.dumps(config, indent=2))

    busy = [port for port in [*ports, args.port] if port_in_use(port)]
    if busy:
        sys.exit(
            f'ports already in use: {", ".join(map(str, busy))} (a previous dev stack still running? '
            f'see `fuser {" ".join(f"{p}/tcp" for p in busy)}`, or pick other ports with --port/--fake-port)'
        )

    # The signal handler only sets a flag; shutdown work runs in the main loop below.
    stop_requested = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    children: list[subprocess.Popen] = []
    exited = None
    try:
        for name, port in zip(names, ports, strict=True):
            children.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        '-m',
                        'logserver.fake.main',
                        '--name',
                        name,
                        '--host',
                        '127.0.0.1',
                        '--port',
                        str(port),
                        '--dir',
                        str(workdir / name),
                        '--log-level',
                        'warning',
                    ],
                    env=CHILD_ENV,
                )
            )
        for port in ports:
            wait_http(f'http://127.0.0.1:{port}/v1/info')
        children.append(
            subprocess.Popen(
                [
                    sys.executable,
                    '-m',
                    'log_master.cli.main',
                    'serve',
                    '--config',
                    str(config_path),
                    '--log-level',
                    'warning',
                ],
                env=CHILD_ENV,
            )
        )
        wait_http(f'http://127.0.0.1:{args.port}/api/v1/state')
        sys.stderr.write(f'dev stack up: http://127.0.0.1:{args.port}  (working directory {workdir})\n')
        while not stop_requested and exited is None:
            exited = next((child for child in children if child.poll() is not None), None)
            time.sleep(0.2)
        if exited is not None:
            sys.stderr.write(f'{" ".join(exited.args[2:5])} exited with {exited.returncode}; stopping the dev stack\n')
    finally:
        stop_children(children)
    sys.exit(0 if exited is None else 1)


if __name__ == '__main__':
    main()
