"""Serving the prebuilt SvelteKit SPA with an `index.html` fallback for client-side routes."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from logserver.errors import ApiError

STATIC_DIR = Path(__file__).parent / 'static'
_IMMUTABLE_PREFIX = '_app/immutable/'


def mount_spa(app: FastAPI, static_dir: Path = STATIC_DIR) -> None:
    """Serve files from `static_dir` and fall back to `index.html` for all other non-API GET paths.

    Must be called after all API routes are registered.
    """
    root = static_dir.resolve()

    @app.get('/{path:path}', include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        if path == 'api' or path.startswith('api/'):
            raise ApiError(404, 'not_found', f'no API endpoint /{path}')
        index = root / 'index.html'
        if not index.is_file():
            raise ApiError(404, 'frontend_not_built', 'the web UI has not been built (run `make frontend`)')
        candidate = (root / path).resolve()
        if path and candidate.is_relative_to(root) and candidate.is_file():
            cache = 'public, max-age=31536000, immutable' if path.startswith(_IMMUTABLE_PREFIX) else 'no-cache'
            return FileResponse(candidate, headers={'cache-control': cache})
        return FileResponse(index, headers={'cache-control': 'no-cache'})
