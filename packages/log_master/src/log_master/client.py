"""HTTP calls from log-master to logging servers."""

from typing import Any

import httpx
from logserver.models import ErrorEnvelope


class SourceUnreachable(Exception):
    """The source did not answer (connection error or timeout)."""


class SourceRequestError(Exception):
    """The source answered with a non-2xx status."""

    def __init__(self, status_code: int, code: str, message: str, details: dict[str, Any]):
        super().__init__(f'{status_code} {code}: {message}')
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details

    @classmethod
    def from_response(cls, response: httpx.Response) -> 'SourceRequestError':
        """Build the error from a response carrying (ideally) the standard error envelope."""
        try:
            error = ErrorEnvelope.model_validate_json(response.content).error
        except ValueError:
            return cls(response.status_code, 'invalid_response', response.text[:200] or response.reason_phrase, {})
        return cls(response.status_code, error.code, error.message, error.details)


def describe_transport_error(exc: httpx.HTTPError) -> str:
    """Return a short human-readable description of a transport error."""
    if isinstance(exc, httpx.TimeoutException):
        return 'timeout'
    return str(exc) or type(exc).__name__


async def call_source(
    http: httpx.AsyncClient,
    base_url: str,
    method: str,
    path: str,
    *,
    timeout: float,
    json: Any = None,
) -> httpx.Response:
    """Call `<base_url>/v1<path>` and return the 2xx response.

    Raises:
        SourceUnreachable: On connection errors and timeouts.
        SourceRequestError: On non-2xx responses.
    """
    try:
        response = await http.request(method, f'{base_url}/v1{path}', json=json, timeout=timeout)
    except httpx.HTTPError as exc:
        raise SourceUnreachable(describe_transport_error(exc)) from exc
    if response.status_code >= 400:
        raise SourceRequestError.from_response(response)
    return response
