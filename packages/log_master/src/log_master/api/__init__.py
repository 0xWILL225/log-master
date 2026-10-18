"""REST routers of the log-master API (`/api/v1`)."""

from fastapi import APIRouter

from log_master.api import logs, recording, settings, sources, state


def create_api_router() -> APIRouter:
    """Combine all routers under `/api/v1`."""
    router = APIRouter(prefix='/api/v1')
    for module in (state, recording, logs, sources, settings):
        router.include_router(module.router)
    return router
