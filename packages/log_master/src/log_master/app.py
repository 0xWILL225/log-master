"""FastAPI app factory of the log-master daemon."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from logserver.errors import install_error_handlers

from log_master import __version__
from log_master.api import create_api_router
from log_master.config import ConfigFile
from log_master.runtime import Runtime
from log_master.static import STATIC_DIR, mount_spa

logger = logging.getLogger(__name__)


def build_app(static_dir: Path = STATIC_DIR) -> FastAPI:
    """Build the app (routes, error handlers, SPA) without a runtime; used for OpenAPI generation."""
    app = FastAPI(
        title='log-master API',
        version=__version__,
        description='REST API of the log-master daemon. Errors use the same envelope as the Logging Server API.',
    )
    install_error_handlers(app)
    app.include_router(create_api_router())
    mount_spa(app, static_dir)
    return app


def create_app(config_path: Path | str, static_dir: Path = STATIC_DIR) -> FastAPI:
    """Create the daemon app for `config_path`; polling starts with the app's lifespan.

    The config file is loaded immediately so that configuration errors surface before the
    server binds its port.
    """
    config_file = ConfigFile(config_path)
    app = build_app(static_dir)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        runtime = Runtime.create(config_file)
        app.state.runtime = runtime
        poll_task = asyncio.create_task(runtime.poller.run(), name='poller')
        logger.info('log-master %s started with %d sources', __version__, len(runtime.config.sources))
        try:
            yield
        finally:
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task
            await runtime.http.aclose()

    app.router.lifespan_context = lifespan
    return app
