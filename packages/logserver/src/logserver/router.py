"""FastAPI implementation of all `/v1` endpoints of the Logging Server API.

A server provides a :class:`Backend` and gets a complete, spec-conforming HTTP API::

    app = create_app(backend)
    run_in_thread(app, host='0.0.0.0', port=8001)

Handlers are synchronous functions, so uvicorn runs them in its worker thread pool; they
only touch the :class:`~logserver.session.LogSession` (which is thread-safe) and the
backend methods listed in :class:`Backend`, which must be thread-safe as well.
"""

from pathlib import PurePosixPath
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Body, FastAPI
from fastapi.responses import FileResponse

from logserver.config import ConfigStore, validate_json_schema
from logserver.errors import ApiError, error_responses, install_error_handlers
from logserver.models import (
    PROTOCOL_VERSION,
    ActionList,
    ActionResult,
    ActionSpec,
    ConfigDocument,
    Detail,
    Health,
    Info,
    LogDetail,
    LogList,
    Recording,
    StartRequest,
    Status,
    StopRequest,
)
from logserver.session import LogSession, SegmentHandler

_LEVEL_RANK = {'ok': 0, 'warn': 1, 'error': 2}


class Backend(SegmentHandler, Protocol):
    """What a logging server implements to get the HTTP API from :func:`create_router`.

    Besides the attributes and methods below, the backend is the session's
    :class:`~logserver.session.SegmentHandler` (`open_segment` / `close_segment`).
    """

    info: Info
    session: LogSession
    config: ConfigStore[Any]

    def health(self) -> Health:
        """Return the server's own view of its health (device connected, fix, ...)."""
        ...

    def details(self) -> list[Detail]:
        """Return the ordered key/value rows shown on the source's status card. Must be cheap."""
        ...

    def actions(self) -> list[ActionSpec]:
        """Return the device-specific actions, with their current availability."""
        ...

    def run_action(self, name: str, params: dict[str, Any]) -> ActionResult:
        """Run action `name` with schema-validated `params`. Must return promptly."""
        ...


def combined_health(backend: Backend) -> Health:
    """Combine the backend's health with session write faults and pending restarts (worst level wins)."""
    candidates = [backend.health()]
    fault = backend.session.fault
    if fault is not None:
        candidates.append(Health(level='error', message=fault))
    pending = backend.config.restart_pending
    if pending:
        candidates.append(Health(level='warn', message=f'restart required to apply: {", ".join(pending)}'))
    return max(candidates, key=lambda health: _LEVEL_RANK[health.level])


def create_router(backend: Backend) -> APIRouter:
    """Create the `/v1` router of the Logging Server API for `backend`."""
    router = APIRouter(prefix='/v1', tags=['logging server'])

    @router.get('/info', summary='Static identity of the server')
    def get_info() -> Info:
        return backend.info

    @router.get('/status', summary='Cheap status snapshot, polled at about 1 Hz')
    def get_status() -> Status:
        return Status(
            health=combined_health(backend),
            recording=backend.session.recording(),
            disk=backend.session.disk(),
            details=backend.details(),
        )

    @router.post('/log/start', summary='Start (or rejoin) a log', responses=error_responses(400, 409))
    def start_log(request: StartRequest | None = None) -> Recording:
        return backend.session.start(request or StartRequest())

    @router.post('/log/stop', summary='Stop the current log', responses=error_responses(400, 409))
    def stop_log(request: StopRequest | None = None) -> Recording:
        return backend.session.stop(request or StopRequest())

    @router.get('/logs', summary='List logs, newest first')
    def list_logs() -> LogList:
        return LogList(logs=backend.session.list_logs())

    @router.get('/logs/{log_id}', summary='One log including its manifest', responses=error_responses(404))
    def get_log(log_id: str) -> LogDetail:
        return backend.session.get_log(log_id)

    @router.get(
        '/logs/{log_id}/files/{path:path}',
        summary='Download a file of a log (supports Range)',
        response_class=FileResponse,
        responses={200: {'content': {'application/octet-stream': {}}}, 206: {'description': 'Partial Content'}}
        | error_responses(404, 409, 416),
    )
    def get_file(log_id: str, path: str) -> FileResponse:
        if backend.session.is_recording(log_id):
            raise ApiError(409, 'recording_in_progress', f'log {log_id} is being recorded')
        file_path = backend.session.resolve_file(log_id, path)
        return FileResponse(file_path, filename=PurePosixPath(path).name)

    @router.get('/config', summary='Config schema and values')
    def get_config() -> ConfigDocument:
        return backend.config.document()

    @router.patch('/config', summary='Partially update the config', responses=error_responses(400, 409, 422))
    def patch_config(patch: Annotated[dict[str, Any], Body(examples=[{'pps_interval_ms': 500}])]) -> ConfigDocument:
        if backend.session.active_log_id is not None:
            raise ApiError(409, 'recording_in_progress', 'config cannot be changed while recording')
        return backend.config.patch(patch)

    @router.get('/actions', summary='Self-described device-specific actions')
    def list_actions() -> ActionList:
        return ActionList(actions=backend.actions())

    @router.post('/actions/{name}', summary='Run an action', responses=error_responses(400, 404, 409, 422))
    def run_action(name: str, params: Annotated[dict[str, Any] | None, Body()] = None) -> ActionResult:
        spec = next((action for action in backend.actions() if action.name == name), None)
        if spec is None:
            raise ApiError(404, 'action_not_found', f'no action {name!r}')
        if not spec.available:
            raise ApiError(409, 'action_unavailable', spec.unavailable_reason or f'action {name!r} is unavailable')
        params = params or {}
        validate_json_schema(spec.params_schema, params, f'parameters of action {name!r}')
        return backend.run_action(name, params)

    return router


def create_app(backend: Backend, *, title: str = 'Logging Server API') -> FastAPI:
    """Create a FastAPI app serving the Logging Server API for `backend`, with error envelopes."""
    app = FastAPI(
        title=title,
        version=str(PROTOCOL_VERSION),
        description='Normative HTTP API that every log-master logging server implements.',
    )
    install_error_handlers(app)
    app.include_router(create_router(backend))
    return app


class _SpecOnlyBackend:
    """Placeholder backend for generating the OpenAPI document; any use is a bug."""

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f'backend attribute {name!r} used while generating the OpenAPI document')


def openapi_schema() -> dict[str, Any]:
    """Return the OpenAPI document of the Logging Server API (source of `spec/logserver.openapi.yaml`)."""
    return create_app(_SpecOnlyBackend()).openapi()  # type: ignore[arg-type]
