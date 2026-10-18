"""Human-readable formatting for CLI output."""

import json
import sys
from datetime import datetime
from typing import Any


def print_json(data: Any) -> None:
    """Write `data` as indented JSON to stdout (machine-readable, no colours)."""
    sys.stdout.write(json.dumps(data, indent=2) + '\n')


def format_bytes(size: float | None) -> str:
    """Format a byte count with a binary unit, e.g. `4.2 GiB`."""
    if size is None:
        return '-'
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if abs(size) < 1024 or unit == 'TiB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    raise AssertionError('unreachable')


def format_duration_ns(duration_ns: int | None) -> str:
    """Format a duration as `h:mm:ss`."""
    if duration_ns is None:
        return '-'
    seconds = max(0, round(duration_ns / 1e9))
    return f'{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}'


def format_time_ns(unix_ns: int | None) -> str:
    """Format a timestamp in local time, e.g. `2026-10-17 16:03:55`."""
    if unix_ns is None:
        return '-'
    return datetime.fromtimestamp(unix_ns / 1e9).astimezone().strftime('%Y-%m-%d %H:%M:%S')


def parse_time(text: str) -> int:
    """Parse an ISO date or date-time (local time unless an offset is given) into unix nanoseconds.

    Raises:
        ValueError: If `text` is not an ISO date/time.
    """
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return int(moment.timestamp() * 1e9)
