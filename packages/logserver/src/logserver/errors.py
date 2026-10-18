"""The JSON error envelope shared by the Logging Server API and the log-master REST API.

Every non-2xx response carries `{"error": {"code": ..., "message": ..., "details": {...}}}`.
Raise :class:`ApiError` from a handler and install the handlers with
:func:`install_error_handlers` to get this behaviour.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from logserver.models import ErrorEnvelope

logger = logging.getLogger(__name__)

_HTTP_STATUS_CODES: dict[int, str] = {
    400: 'bad_request',
    404: 'not_found',
    405: 'method_not_allowed',
    409: 'conflict',
    416: 'range_not_satisfiable',
    422: 'validation_error',
    500: 'internal',
    503: 'not_ready',
}


class ApiError(Exception):
    """An error that is reported to the client with the standard error envelope."""

    def __init__(self, status_code: int, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}

    def envelope(self) -> dict[str, Any]:
        """Return the JSON-serializable error envelope."""
        return {'error': {'code': self.code, 'message': self.message, 'details': jsonable_encoder(self.details)}}


def error_response(status_code: int, code: str, message: str, details: dict[str, Any] | None = None) -> JSONResponse:
    """Build a JSON response carrying the standard error envelope."""
    return JSONResponse(ApiError(status_code, code, message, details).envelope(), status_code=status_code)


def error_responses(*status_codes: int) -> dict[int | str, dict[str, Any]]:
    """Build a FastAPI `responses=` mapping documenting the error envelope for `status_codes`."""
    return {
        status: {'model': ErrorEnvelope, 'description': _HTTP_STATUS_CODES.get(status, 'error')}
        for status in status_codes
    }


def install_error_handlers(app: FastAPI) -> None:
    """Make `app` report all errors (including FastAPI/Starlette ones) with the error envelope."""

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.envelope(), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _request_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = jsonable_encoder(exc.errors(), exclude={'ctx', 'url'})
        return error_response(400, 'bad_request', 'malformed request', {'errors': errors})

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_STATUS_CODES.get(exc.status_code, 'http_error')
        return error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception('unhandled error')
        return error_response(500, 'internal', f'{type(exc).__name__}: {exc}')
