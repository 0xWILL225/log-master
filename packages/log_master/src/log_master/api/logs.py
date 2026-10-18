"""Log listing and downloads."""

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import StreamingResponse
from logserver.errors import error_responses

from log_master import downloads, listing
from log_master.api.deps import RuntimeDep
from log_master.models import LogListResponse, LogResponse

router = APIRouter(prefix='/logs', tags=['logs'])


@router.get('', summary='Merged log list, newest first')
async def list_logs(
    runtime: RuntimeDep,
    since: int | None = Query(None, description='Only logs starting at or after this time (unix ns).'),
    until: int | None = Query(None, description='Only logs starting before this time (unix ns).'),
    source: str | None = Query(None, description='Only logs with a part from this source.'),
) -> LogListResponse:
    """Fetch `/v1/logs` from all online sources and merge entries by log ID."""
    return await listing.list_logs(runtime, since=since, until=until, source=source)


@router.get('/{log_id}', summary='One merged log with manifests', responses=error_responses(404))
async def get_log(log_id: str, runtime: RuntimeDep) -> LogResponse:
    """Return one log with its per-source parts and manifests."""
    return await listing.get_log(runtime, log_id)


@router.get(
    '/{log_id}/download.zip',
    summary='Whole log as a streaming ZIP',
    response_class=StreamingResponse,
    responses={200: {'content': {'application/zip': {}}}} | error_responses(404, 409, 503),
)
async def download_zip(
    log_id: str,
    runtime: RuntimeDep,
    sources: str | None = Query(None, description='Comma-separated source names (default: all).'),
) -> StreamingResponse:
    """Stream `<log_id>.zip` (stored, ZIP64) with layout `<log_id>/<source>/<files>`."""
    names = [name for name in sources.split(',') if name] if sources else None
    return await downloads.zip_log(runtime, log_id, names)


@router.get(
    '/{log_id}/sources/{source}/files/{path:path}',
    summary='Proxied single-file download (supports Range)',
    response_class=StreamingResponse,
    responses={200: {'content': {'application/octet-stream': {}}}, 206: {'description': 'Partial Content'}}
    | error_responses(404, 409, 416, 503),
)
async def download_file(log_id: str, source: str, path: str, request: Request, runtime: RuntimeDep) -> Response:
    """Stream one file from a source; resumable with `Range`/`If-Range`."""
    return await downloads.proxy_file(runtime, log_id, source, path, request)
