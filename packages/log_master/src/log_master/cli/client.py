"""HTTP client used by the CLI, with the CLI's exit-code conventions."""

from typing import Any

import httpx

EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CONFLICT = 3
EXIT_UNREACHABLE = 4

DEFAULT_URL = 'http://localhost:8080'


class CliError(Exception):
    """An error reported to the user (without traceback) with a specific exit code."""

    def __init__(self, message: str, exit_code: int = EXIT_ERROR):
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


class ApiCallError(CliError):
    """log-master answered with an error envelope."""

    def __init__(self, status_code: int, code: str, message: str, details: dict[str, Any]):
        super().__init__(f'{message} ({status_code} {code})')
        self.status_code = status_code
        self.code = code
        self.api_message = message
        self.details = details


class Api:
    """Synchronous client of the log-master REST API (`<url>/api/v1`)."""

    def __init__(self, url: str, timeout: float = 30.0):
        self.url = url.rstrip('/')
        self.http = httpx.Client(base_url=f'{self.url}/api/v1', timeout=timeout)

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Send a request and return the decoded JSON body (or `None` for empty bodies).

        Raises:
            CliError: Exit code 4 if log-master is unreachable.
            ApiCallError: For non-2xx responses.
        """
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise CliError(f'log-master unreachable at {self.url}: {exc}', EXIT_UNREACHABLE)
        raise_for_error(response)
        return response.json() if response.content else None

    def get(self, path: str, **kwargs: Any) -> Any:
        """`GET` shortcut for :meth:`request`."""
        return self.request('GET', path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        """`POST` shortcut for :meth:`request`."""
        return self.request('POST', path, **kwargs)


def raise_for_error(response: httpx.Response) -> None:
    """Raise :class:`ApiCallError` for a non-2xx response carrying (ideally) an error envelope."""
    if response.status_code < 400:
        return
    try:
        error = response.json()['error']
        code, message, details = error['code'], error['message'], error.get('details') or {}
    except (ValueError, KeyError, TypeError):
        code, message, details = 'http_error', response.text[:200] or response.reason_phrase, {}
    raise ApiCallError(response.status_code, code, message, details)
