"""FastAPI dependencies shared by the routers."""

from typing import Annotated

from fastapi import Depends, Request

from log_master.runtime import Runtime


def get_runtime(request: Request) -> Runtime:
    """Return the daemon runtime stored on the app by its lifespan."""
    return request.app.state.runtime


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]
