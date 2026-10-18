"""Polling of `GET /v1/status` (and `/v1/info`) on every configured source.

Every `poll.interval_s` all sources are polled concurrently. A source goes offline after
`poll.offline_after_failures` consecutive failures and is online again after one success.
After each round the poll loop drives the recorder (late join, pending stops, timed stop).
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from logserver.models import Info, Recording, Status

from log_master.client import SourceRequestError, SourceUnreachable, call_source
from log_master.config import ConfigFile, SourceConfig

logger = logging.getLogger(__name__)


@dataclass
class SourceRuntime:
    """Live state of one configured source."""

    name: str
    url: str
    enabled_by_default: bool
    online: bool = False
    failures: int = 0
    last_seen_unix_ns: int | None = None
    status: Status | None = None
    info: Info | None = None
    error: str | None = None
    need_info: bool = True

    def update_recording(self, recording_json: object) -> None:
        """Replace the cached recording state with a fresher one (e.g. from a start/stop response)."""
        if self.status is not None:
            self.status = self.status.model_copy(update={'recording': Recording.model_validate(recording_json)})


class Poller:
    """Polls all configured sources and keeps their :class:`SourceRuntime` up to date."""

    def __init__(self, config_file: ConfigFile, http: httpx.AsyncClient, clock: Callable[[], int] = time.time_ns):
        self._config_file = config_file
        self._http = http
        self._clock = clock
        self.sources: dict[str, SourceRuntime] = {}
        self.after_poll: Callable[[], Awaitable[None]] | None = None
        self.sync_sources()

    def sync_sources(self) -> None:
        """Bring :attr:`sources` in line with the configured source list (after config edits)."""
        configured: list[SourceConfig] = self._config_file.config.sources
        updated: dict[str, SourceRuntime] = {}
        for source in configured:
            runtime = self.sources.get(source.name)
            if runtime is None or runtime.url != source.url:
                runtime = SourceRuntime(name=source.name, url=source.url, enabled_by_default=source.enabled_by_default)
            runtime.enabled_by_default = source.enabled_by_default
            updated[source.name] = runtime
        self.sources = updated

    def online(self, name: str) -> bool:
        """Return whether source `name` is configured and online."""
        runtime = self.sources.get(name)
        return runtime is not None and runtime.online

    async def poll_once(self) -> None:
        """Poll every source once, concurrently."""
        await asyncio.gather(*(self._poll(runtime) for runtime in list(self.sources.values())))

    async def run(self) -> None:
        """Poll forever; after every round call :attr:`after_poll`."""
        while True:
            started = time.monotonic()
            try:
                await self.poll_once()
                if self.after_poll is not None:
                    await self.after_poll()
            except Exception:
                logger.exception('poll round failed')
            interval = self._config_file.config.poll.interval_s
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))

    async def _poll(self, runtime: SourceRuntime) -> None:
        poll = self._config_file.config.poll
        try:
            response = await call_source(self._http, runtime.url, 'GET', '/status', timeout=poll.timeout_s)
            status = Status.model_validate_json(response.content)
            if runtime.need_info or runtime.info is None:
                info_response = await call_source(self._http, runtime.url, 'GET', '/info', timeout=poll.timeout_s)
                runtime.info = Info.model_validate_json(info_response.content)
                runtime.need_info = False
        except (SourceUnreachable, SourceRequestError, ValueError) as exc:
            self._record_failure(runtime, exc, poll.offline_after_failures)
            return
        if not runtime.online:
            logger.info('source %s is online', runtime.name)
        runtime.status = status
        runtime.online = True
        runtime.failures = 0
        runtime.error = None
        runtime.last_seen_unix_ns = self._clock()

    @staticmethod
    def _record_failure(runtime: SourceRuntime, exc: Exception, offline_after: int) -> None:
        message = f'invalid response: {exc}' if isinstance(exc, ValueError) else str(exc)
        runtime.failures += 1
        runtime.error = message
        if runtime.online and runtime.failures >= offline_after:
            logger.warning('source %s is offline (%s)', runtime.name, message)
            runtime.online = False
            runtime.need_info = True
