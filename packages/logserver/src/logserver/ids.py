"""Generation and validation of log IDs (`YYYY-MM-DDTHH-MM-SSZ_xxxxxx`)."""

import re
import secrets
import time
from datetime import datetime, timezone

from logserver.models import LOG_ID_PATTERN

_LOG_ID_RE = re.compile(LOG_ID_PATTERN, re.ASCII)
_TIMESTAMP_FORMAT = '%Y-%m-%dT%H-%M-%SZ'


def generate_log_id(unix_ns: int | None = None) -> str:
    """Generate a new log ID from a UTC timestamp and 6 random lowercase hex characters.

    Args:
        unix_ns: Time to encode (nanoseconds since the Unix epoch). Defaults to the current
            `CLOCK_REALTIME` time of this host.

    Returns:
        A log ID such as `2026-10-17T14-03-55Z_a1b2c3`.
    """
    if unix_ns is None:
        unix_ns = time.time_ns()
    timestamp = datetime.fromtimestamp(unix_ns // 1_000_000_000, tz=timezone.utc)
    return f'{timestamp.strftime(_TIMESTAMP_FORMAT)}_{secrets.token_hex(3)}'


def is_valid_log_id(value: str) -> bool:
    """Return whether `value` is a syntactically valid log ID."""
    return _LOG_ID_RE.fullmatch(value) is not None


def log_id_timestamp_ns(log_id: str) -> int:
    """Return the timestamp encoded in a log ID as nanoseconds since the Unix epoch.

    Raises:
        ValueError: If `log_id` is not a valid log ID.
    """
    if not is_valid_log_id(log_id):
        raise ValueError(f'invalid log ID: {log_id!r}')
    timestamp = datetime.strptime(log_id[:20], _TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    return int(timestamp.timestamp()) * 1_000_000_000
