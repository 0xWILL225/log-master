"""`GET /api/v1/state`: the aggregate snapshot polled by the UI."""

from fastapi import APIRouter

from log_master.api.deps import RuntimeDep
from log_master.models import StateSnapshot

router = APIRouter(tags=['state'])


@router.get('/state', summary='Aggregate snapshot polled by the UI at 1 Hz')
async def get_state(runtime: RuntimeDep) -> StateSnapshot:
    """Return global recording state, the active log, per-source summaries and warnings."""
    return runtime.recorder.snapshot()
