"""Shared CLI plumbing: global options, the API client and the console."""

from dataclasses import dataclass
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from log_master.cli.client import DEFAULT_URL, Api

console = Console(highlight=False)

JsonOpt = Annotated[bool, typer.Option('--json', help='Machine-readable JSON output.')]


@dataclass
class GlobalOptions:
    """Options given before the command (`log-master --url ... --json <command>`)."""

    url: str = DEFAULT_URL
    json: bool = False


def setup(ctx: typer.Context, as_json: bool) -> tuple[Api, bool]:
    """Return the API client and whether JSON output was requested (globally or per command)."""
    options = ctx.find_root().obj or GlobalOptions()
    return Api(options.url), options.json or as_json


def split_csv(values: list[str] | None) -> list[str]:
    """Split repeated and/or comma-separated option values into a flat list."""
    return [item.strip() for value in values or [] for item in value.split(',') if item.strip()]


def styled(text: object, level: str | None) -> str:
    """Return `text` as escaped Rich markup, coloured by a health/detail level if given."""
    style = {'ok': 'green', 'warn': 'yellow', 'error': 'bold red'}.get(level or '')
    escaped = escape(str(text))
    return f'[{style}]{escaped}[/]' if style else escaped
