"""Non-source settings of `config.yaml`."""

from typing import Annotated, Any

from fastapi import APIRouter, Body
from logserver.errors import error_responses
from logserver.models import ConfigDocument

from log_master.api.deps import RuntimeDep

router = APIRouter(prefix='/settings', tags=['settings'])


@router.get('', summary='Settings schema and values')
async def get_settings(runtime: RuntimeDep) -> ConfigDocument:
    """Return `{schema, values}` of everything in config.yaml except the source list."""
    return runtime.config_file.settings_document()


@router.patch('', summary='Partially update settings', responses=error_responses(400, 422))
async def patch_settings(
    runtime: RuntimeDep, patch: Annotated[dict[str, Any], Body(examples=[{'poll': {'interval_s': 0.5}}])]
) -> ConfigDocument:
    """Validate, persist (comments preserved) and apply settings live; `http.bind` needs a restart."""
    return runtime.config_file.patch_settings(patch)
