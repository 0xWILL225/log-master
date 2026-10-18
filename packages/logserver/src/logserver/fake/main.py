"""`logserver-fake` console script.

Example::

    logserver-fake --name fake1 --port 8101 --dir /tmp/fake1 --rate 200kB/s
"""

import argparse
import asyncio
import logging
import re
from pathlib import Path

from fastapi import FastAPI, Request, Response

from logserver.config import ConfigStore
from logserver.fake.backend import FakeBackend, FakeConfig
from logserver.router import create_app
from logserver.runner import StopFlag, run_in_thread

logger = logging.getLogger('logserver.fake')

_RATE_RE = re.compile(r'^\s*(\d+(?:\.\d+)?)\s*(B|kB|KiB|MB|MiB)?(?:/s)?\s*$')
_RATE_UNITS = {None: 1, 'B': 1, 'kB': 1000, 'KiB': 1024, 'MB': 1_000_000, 'MiB': 1024 * 1024}


def parse_rate(text: str) -> int:
    """Parse a data rate such as `200kB/s`, `1.5MiB` or `5000` into bytes per second.

    Raises:
        argparse.ArgumentTypeError: If `text` is not a valid rate.
    """
    match = _RATE_RE.match(text)
    if match is None:
        raise argparse.ArgumentTypeError(f'invalid rate {text!r} (examples: 200kB/s, 1MiB/s, 5000)')
    rate = int(float(match.group(1)) * _RATE_UNITS[match.group(2)])
    if rate < 1:
        raise argparse.ArgumentTypeError('rate must be at least 1 B/s')
    return rate


def create_fake_app(backend: FakeBackend) -> FastAPI:
    """Create the HTTP app of the fake, including the `go-offline` fault injection."""
    app = create_app(backend, title='logserver-fake')

    @app.middleware('http')
    async def simulate_offline(request: Request, call_next) -> Response:
        remaining = backend.offline_remaining()
        if remaining > 0:
            await asyncio.sleep(remaining)
        return await call_next(request)

    return app


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        prog='logserver-fake', description='Fake logging server for development and tests.'
    )
    parser.add_argument('--name', default='fake', help='server name (default: %(default)s)')
    parser.add_argument('--host', default='0.0.0.0', help='bind host (default: %(default)s)')
    parser.add_argument('--port', type=int, default=8101, help='bind port (default: %(default)s)')
    parser.add_argument('--dir', type=Path, help='log directory (default: /tmp/logserver-fake/<name>)')
    parser.add_argument('--config', type=Path, help='config file (default: <dir>/config.yaml)')
    parser.add_argument('--rate', type=parse_rate, help='data rate, e.g. 200kB/s (default: from config)')
    parser.add_argument('--units-per-s', type=int, help='data units per second (default: from config)')
    parser.add_argument('--slow-start-ms', type=int, help='delay of the first written unit after the start time')
    parser.add_argument('--health', choices=['ok', 'warn', 'error'], help='health level override')
    parser.add_argument('--log-level', default='info', choices=['debug', 'info', 'warning', 'error'])
    return parser


def main(argv: list[str] | None = None) -> None:
    """Run the fake logging server until SIGINT/SIGTERM."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    log_dir = (args.dir or Path('/tmp/logserver-fake') / args.name).absolute()
    log_dir.mkdir(parents=True, exist_ok=True)
    config = ConfigStore(FakeConfig, args.config or log_dir / 'config.yaml')
    overrides: dict[str, object] = {
        'name': args.name,
        'log_directory': str(log_dir),
        'bind_address': f'{args.host}:{args.port}',
    }
    if args.rate is not None:
        overrides['rate_bytes_per_s'] = args.rate
    if args.units_per_s is not None:
        overrides['units_per_s'] = args.units_per_s
    if args.slow_start_ms is not None:
        overrides['slow_start_ms'] = args.slow_start_ms
    if args.health is not None:
        overrides['health_override'] = args.health
    config.force_update(overrides)

    backend = FakeBackend(config)
    stop = StopFlag().install()
    server = run_in_thread(create_fake_app(backend), args.host, args.port, log_level=args.log_level)
    logger.info('%s listening on http://%s:%d, logs in %s', args.name, args.host, args.port, log_dir)
    try:
        backend.run_io_loop(stop)
    finally:
        backend.session.close()
        server.stop()
        logger.info('%s stopped', args.name)


if __name__ == '__main__':
    main()
