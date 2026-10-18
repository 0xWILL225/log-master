"""Download proxy (spec §6.8): resumable single files and streaming whole-log ZIPs.

All bytes to browsers flow through log-master; browsers never talk to sources directly.
"""

import re
from collections.abc import Iterator
from urllib.parse import quote

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse
from logserver.errors import ApiError
from starlette.background import BackgroundTask
from zipstream import ZipStream

from log_master.client import describe_transport_error
from log_master.listing import fetch_parts
from log_master.poller import SourceRuntime
from log_master.runtime import Runtime

_FORWARDED_REQUEST_HEADERS = ('range', 'if-range')
_FORWARDED_RESPONSE_HEADERS = (
    'content-length',
    'content-range',
    'accept-ranges',
    'etag',
    'last-modified',
    'content-type',
)
_UNSAFE_FILENAME_CHARS = re.compile(r'[^A-Za-z0-9._-]')
_CHUNK_SIZE = 256 * 1024


def _online_source(runtime: Runtime, name: str) -> SourceRuntime:
    source = runtime.poller.sources.get(name)
    if source is None:
        raise ApiError(404, 'source_not_found', f'no source {name!r}')
    if not source.online:
        raise ApiError(503, 'source_offline', f'source {name} is offline', {'source': name})
    return source


def _file_url(source: SourceRuntime, log_id: str, path: str) -> str:
    return f'{source.url}/v1/logs/{quote(log_id)}/files/{quote(path, safe="/")}'


def attachment_filename(log_id: str, source: str, path: str) -> str:
    """Return the download file name `<log_id>_<source>_<path>` with unsafe characters replaced."""
    return _UNSAFE_FILENAME_CHARS.sub('_', f'{log_id}_{source}_{path.replace("/", "_")}')


async def proxy_file(runtime: Runtime, log_id: str, source_name: str, path: str, request: Request) -> Response:
    """Stream one file from a source, forwarding `Range`/`If-Range` so downloads are resumable."""
    source = _online_source(runtime, source_name)
    headers = {name: request.headers[name] for name in _FORWARDED_REQUEST_HEADERS if name in request.headers}
    timeout = runtime.config.listing.timeout_s
    upstream_request = runtime.http.build_request(
        'GET', _file_url(source, log_id, path), headers=headers, timeout=timeout
    )
    try:
        upstream = await runtime.http.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        raise ApiError(503, 'source_offline', f'source {source_name} is unreachable ({describe_transport_error(exc)})')
    if upstream.status_code >= 400:
        body = await upstream.aread()
        await upstream.aclose()
        return Response(body, status_code=upstream.status_code, media_type=upstream.headers.get('content-type'))
    response_headers = {
        name: upstream.headers[name] for name in _FORWARDED_RESPONSE_HEADERS if name in upstream.headers
    }
    response_headers['content-disposition'] = f'attachment; filename="{attachment_filename(log_id, source_name, path)}"'
    return StreamingResponse(
        upstream.aiter_raw(_CHUNK_SIZE),
        status_code=upstream.status_code,
        headers=response_headers,
        background=BackgroundTask(upstream.aclose),
    )


def _stream_file(client: httpx.Client, url: str, expected_size: int) -> Iterator[bytes]:
    """Yield a file from a source, failing loudly if its size differs from the listing."""
    received = 0
    with client.stream('GET', url) as response:
        response.raise_for_status()
        for chunk in response.iter_raw(_CHUNK_SIZE):
            received += len(chunk)
            yield chunk
    if received != expected_size:
        raise RuntimeError(f'{url}: expected {expected_size} bytes, received {received}')


async def zip_log(runtime: Runtime, log_id: str, sources: list[str] | None) -> StreamingResponse:
    """Stream a whole log as a stored (uncompressed) ZIP64 archive with a known `Content-Length`.

    Layout: `<log_id>/<source>/<files…>`. If a source fails mid-stream the connection is aborted,
    so the client sees a truncated download rather than a silently incomplete archive.

    Raises:
        ApiError: 404 if no (requested) source has the log, 409 `recording_in_progress`,
            503 `source_offline` for an explicitly requested offline source.
    """
    if sources:
        found, _ = await fetch_parts(runtime, log_id, sources, require_all=True)
    else:
        found, _ = await fetch_parts(runtime, log_id, list(runtime.poller.sources), require_all=False)
    if not found:
        raise ApiError(404, 'log_not_found', f'no reachable source has log {log_id}')
    recording = [name for name, part in found if part.state == 'recording']
    if recording:
        raise ApiError(
            409,
            'recording_in_progress',
            f'log {log_id} is still being recorded by {", ".join(recording)}',
            {'sources': recording},
        )

    client = httpx.Client(timeout=runtime.config.listing.timeout_s)
    archive = ZipStream(sized=True)
    for name, part in found:
        source = runtime.poller.sources[name]
        for entry in part.files:
            archive.add(
                _stream_file(client, _file_url(source, log_id, entry.path), entry.size),
                arcname=f'{log_id}/{name}/{entry.path}',
                size=entry.size,
            )

    def generate() -> Iterator[bytes]:
        try:
            yield from archive
        finally:
            client.close()

    return StreamingResponse(
        generate(),
        media_type='application/zip',
        headers={'content-length': str(len(archive)), 'content-disposition': f'attachment; filename="{log_id}.zip"'},
        background=BackgroundTask(client.close),
    )
