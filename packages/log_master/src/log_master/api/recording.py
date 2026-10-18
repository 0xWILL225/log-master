"""Recording control: start, stop, dismiss pending stops."""

from fastapi import APIRouter, Response
from logserver.errors import error_responses

from log_master.api.deps import RuntimeDep
from log_master.models import StartErrorEnvelope, StartRecordingRequest, StartRecordingResponse, StopRecordingResponse

router = APIRouter(prefix='/recording', tags=['recording'])


@router.post(
    '/start',
    summary='Start a synchronized log',
    responses=error_responses(400)
    | {409: {'model': StartErrorEnvelope, 'description': 'conflicts or already_recording'}},
)
async def start_recording(request: StartRecordingRequest, runtime: RuntimeDep) -> StartRecordingResponse:
    """Start a log on the selected sources; `409 conflicts` lists busy sources to take over or skip."""
    return await runtime.recorder.start(request)


@router.post('/stop', summary='Stop the active log', responses=error_responses(409))
async def stop_recording(runtime: RuntimeDep) -> StopRecordingResponse:
    """Stop the active log; sources that do not confirm get a pending stop."""
    return await runtime.recorder.stop()


@router.post(
    '/dismiss-pending/{source}', status_code=204, summary='Forget a pending stop', responses=error_responses(404)
)
async def dismiss_pending(source: str, runtime: RuntimeDep) -> Response:
    """Stop retrying the pending stop of `source`."""
    await runtime.recorder.dismiss_pending(source)
    return Response(status_code=204)
