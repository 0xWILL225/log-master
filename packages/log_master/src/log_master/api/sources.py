"""Source list editing and proxied per-source config/actions."""

from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Body, Response
from logserver.errors import ApiError, error_responses
from logserver.models import ActionList, ActionResult, ConfigDocument

from log_master.api.deps import RuntimeDep
from log_master.client import describe_transport_error
from log_master.config import SourceConfig, SourceUpdate
from log_master.models import SourceList, SourceSummary
from log_master.runtime import Runtime

router = APIRouter(prefix='/sources', tags=['sources'])


def _summary(runtime: Runtime, name: str) -> SourceSummary:
    source = runtime.poller.sources.get(name)
    if source is None:
        raise ApiError(404, 'source_not_found', f'no source {name!r}')
    return runtime.recorder.source_summary(source)


async def _proxy(runtime: Runtime, name: str, method: str, path: str, body: Any = None) -> Response:
    """Forward a JSON request to a source and return its response (including error envelopes) unchanged."""
    source = runtime.poller.sources.get(name)
    if source is None:
        raise ApiError(404, 'source_not_found', f'no source {name!r}')
    if not source.online:
        raise ApiError(503, 'source_offline', f'source {name} is offline', {'source': name})
    try:
        upstream = await runtime.http.request(
            method, f'{source.url}/v1{path}', json=body, timeout=runtime.config.listing.timeout_s
        )
    except httpx.HTTPError as exc:
        raise ApiError(503, 'source_offline', f'source {name} is unreachable ({describe_transport_error(exc)})')
    return Response(
        upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get('content-type', 'application/json'),
    )


@router.get('', summary='Configured sources with live summary')
async def list_sources(runtime: RuntimeDep) -> SourceList:
    """Return all configured sources in config order."""
    return SourceList(sources=[runtime.recorder.source_summary(s) for s in runtime.poller.sources.values()])


@router.post('', status_code=201, summary='Add a source', responses=error_responses(409, 422))
async def add_source(source: SourceConfig, runtime: RuntimeDep) -> SourceSummary:
    """Append a source to config.yaml. It does not join an active log."""
    runtime.config_file.add_source(source)
    runtime.config_changed()
    return _summary(runtime, source.name)


@router.get('/{name}', summary='Info and full status of a source', responses=error_responses(404))
async def get_source(name: str, runtime: RuntimeDep) -> SourceSummary:
    """Return the live summary of one source, including `/v1/info` and the last `/v1/status`."""
    return _summary(runtime, name)


@router.put('/{name}', summary='Edit a source', responses=error_responses(404, 409, 422))
async def update_source(name: str, update: SourceUpdate, runtime: RuntimeDep) -> SourceSummary:
    """Change URL, `enabled_by_default` or name. Active participants cannot be renamed."""
    renaming = update.name is not None and update.name != name
    if renaming and (runtime.recorder.is_participant(name) or name in runtime.recorder.stop_pending):
        raise ApiError(409, 'source_in_use', f'source {name} takes part in the active log or has a pending stop')
    updated = runtime.config_file.update_source(name, update)
    runtime.config_changed()
    return _summary(runtime, updated.name)


@router.delete('/{name}', status_code=204, summary='Remove a source', responses=error_responses(404, 409))
async def remove_source(name: str, runtime: RuntimeDep) -> Response:
    """Remove a source from config.yaml. Active participants cannot be removed."""
    if runtime.recorder.is_participant(name):
        raise ApiError(409, 'source_in_use', f'source {name} takes part in the active log')
    runtime.config_file.remove_source(name)
    runtime.recorder.forget_source(name)
    runtime.config_changed()
    return Response(status_code=204)


@router.get(
    '/{name}/config',
    response_model=ConfigDocument,
    summary='Proxied GET /v1/config',
    responses=error_responses(404, 503),
)
async def get_source_config(name: str, runtime: RuntimeDep) -> Response:
    """Return the source's config schema and values."""
    return await _proxy(runtime, name, 'GET', '/config')


@router.patch(
    '/{name}/config',
    response_model=ConfigDocument,
    summary='Proxied PATCH /v1/config',
    responses=error_responses(404, 409, 422, 503),
)
async def patch_source_config(name: str, runtime: RuntimeDep, patch: Annotated[dict[str, Any], Body()]) -> Response:
    """Send a partial config update to the source."""
    return await _proxy(runtime, name, 'PATCH', '/config', patch)


@router.get(
    '/{name}/actions', response_model=ActionList, summary='Proxied GET /v1/actions', responses=error_responses(404, 503)
)
async def list_source_actions(name: str, runtime: RuntimeDep) -> Response:
    """Return the source's self-described actions."""
    return await _proxy(runtime, name, 'GET', '/actions')


@router.post(
    '/{name}/actions/{action}',
    response_model=ActionResult,
    summary='Proxied POST /v1/actions/{action}',
    responses=error_responses(404, 409, 422, 503),
)
async def run_source_action(
    name: str, action: str, runtime: RuntimeDep, params: Annotated[dict[str, Any] | None, Body()] = None
) -> Response:
    """Run an action on the source with the given parameters."""
    return await _proxy(runtime, name, 'POST', f'/actions/{action}', params or {})
