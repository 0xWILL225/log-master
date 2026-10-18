"""Aggregation of log listings across sources (spec §6.7).

`GET /v1/logs` is fetched from all online sources concurrently and entries are merged by
log ID. Unreachable sources are reported, never waited for beyond `listing.timeout_s`.
"""

import asyncio

from logserver.errors import ApiError
from logserver.ids import log_id_timestamp_ns
from logserver.models import LogDetail, LogEntry, LogList

from log_master.client import SourceRequestError, SourceUnreachable, call_source
from log_master.models import LogListResponse, LogResponse, MergedLog, MergedLogPart
from log_master.runtime import Runtime


def merge_parts(log_id: str, parts: list[MergedLogPart], participants: list[str] | None) -> MergedLog:
    """Merge the per-source parts of one log.

    Args:
        log_id: The log ID.
        parts: One part per source that has the log.
        participants: Participants of the active log if `log_id` is active, else `None`
            (used to flag sources without a part as missing).
    """
    starts = [p.start_unix_ns for p in parts if p.start_unix_ns is not None]
    ends = [p.end_unix_ns for p in parts if p.end_unix_ns is not None]
    start = min(starts) if starts else None
    end = max(ends) if ends else None
    have = {p.source for p in parts}
    missing = [name for name in participants or [] if name not in have]
    return MergedLog(
        log_id=log_id,
        start_unix_ns=start,
        end_unix_ns=end,
        duration_ns=end - start if start is not None and end is not None else None,
        total_bytes=sum(p.total_bytes for p in parts),
        recording=any(p.state == 'recording' for p in parts),
        incomplete=any(p.state == 'interrupted' for p in parts) or bool(missing),
        standalone=any(p.origin == 'standalone' for p in parts),
        gaps=any(len(p.segments) > 1 for p in parts),
        missing_sources=missing,
        parts=sorted(parts, key=lambda p: p.source),
    )


def _part(source: str, entry: LogEntry) -> MergedLogPart:
    manifest = entry.manifest if isinstance(entry, LogDetail) else None
    return MergedLogPart(source=source, **entry.model_dump(exclude={'log_id', 'manifest'}), manifest=manifest)


def _log_time(log: MergedLog) -> int:
    return log.start_unix_ns if log.start_unix_ns is not None else log_id_timestamp_ns(log.log_id)


async def list_logs(
    runtime: Runtime, *, since: int | None = None, until: int | None = None, source: str | None = None
) -> LogListResponse:
    """Return the merged log list, newest first, optionally filtered by time and source."""
    sources = runtime.poller.sources
    if source is not None and source not in sources:
        raise ApiError(404, 'source_not_found', f'no source {source!r}')
    names = [source] if source is not None else list(sources)
    online = [name for name in names if sources[name].online]
    unreachable = [name for name in names if not sources[name].online]
    timeout = runtime.config.listing.timeout_s

    async def fetch(name: str) -> LogList | None:
        try:
            response = await call_source(runtime.http, sources[name].url, 'GET', '/logs', timeout=timeout)
            return LogList.model_validate_json(response.content)
        except (SourceUnreachable, SourceRequestError, ValueError):
            return None

    results = await asyncio.gather(*(fetch(name) for name in online))
    by_id: dict[str, list[MergedLogPart]] = {}
    for name, result in zip(online, results, strict=True):
        if result is None:
            unreachable.append(name)
            continue
        for entry in result.logs:
            by_id.setdefault(entry.log_id, []).append(_part(name, entry))

    active = runtime.recorder.active
    logs = []
    for log_id, parts in by_id.items():
        participants = active.participants if active is not None and active.log_id == log_id else None
        merged = merge_parts(log_id, parts, participants)
        when = _log_time(merged)
        if (since is None or when >= since) and (until is None or when < until):
            logs.append(merged)
    logs.sort(key=lambda log: (_log_time(log), log.log_id), reverse=True)
    return LogListResponse(logs=logs, unreachable_sources=sorted(unreachable))


async def fetch_parts(
    runtime: Runtime, log_id: str, names: list[str], *, require_all: bool
) -> tuple[list[tuple[str, LogDetail]], list[str]]:
    """Fetch `GET /v1/logs/{log_id}` from the given sources.

    Args:
        runtime: The daemon runtime.
        log_id: The log to fetch.
        names: Sources to ask.
        require_all: If true, a source that is offline or lacks the log is an error.

    Returns:
        The `(source, detail)` pairs found and the names of unreachable sources.

    Raises:
        ApiError: 404 `source_not_found` / `log_not_found`, 503 `source_offline` (with `require_all`).
    """
    sources = runtime.poller.sources
    for name in names:
        if name not in sources:
            raise ApiError(404, 'source_not_found', f'no source {name!r}')
    timeout = runtime.config.listing.timeout_s

    async def fetch(name: str) -> LogDetail | str | None:
        if not sources[name].online:
            return 'offline'
        try:
            response = await call_source(runtime.http, sources[name].url, 'GET', f'/logs/{log_id}', timeout=timeout)
            return LogDetail.model_validate_json(response.content)
        except SourceRequestError as exc:
            if exc.status_code == 404:
                return None
            return f'{exc.code}: {exc.message}'
        except SourceUnreachable as exc:
            return str(exc)

    results = await asyncio.gather(*(fetch(name) for name in names))
    found: list[tuple[str, LogDetail]] = []
    unreachable: list[str] = []
    for name, result in zip(names, results, strict=True):
        if isinstance(result, LogDetail):
            found.append((name, result))
        elif isinstance(result, str):
            if require_all:
                raise ApiError(503, 'source_offline', f'source {name} is unreachable ({result})', {'source': name})
            unreachable.append(name)
        elif require_all:
            raise ApiError(404, 'log_not_found', f'source {name} has no log {log_id}', {'source': name})
    return found, unreachable


async def get_log(runtime: Runtime, log_id: str) -> LogResponse:
    """Return one merged log with per-source parts and manifests.

    Raises:
        ApiError: 404 `log_not_found` if no reachable source has the log.
    """
    found, unreachable = await fetch_parts(runtime, log_id, list(runtime.poller.sources), require_all=False)
    if not found:
        raise ApiError(
            404, 'log_not_found', f'no reachable source has log {log_id}', {'unreachable_sources': unreachable}
        )
    active = runtime.recorder.active
    participants = active.participants if active is not None and active.log_id == log_id else None
    merged = merge_parts(log_id, [_part(name, detail) for name, detail in found], participants)
    return LogResponse(log=merged, unreachable_sources=unreachable)
