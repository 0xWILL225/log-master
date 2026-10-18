"""`log-master settings get | set key=value ...`."""

from typing import Annotated

import typer
from rich.table import Table

from log_master.cli.client import EXIT_USAGE, CliError
from log_master.cli.common import JsonOpt, console, setup
from log_master.cli.format import print_json
from log_master.cli.values import build_patch, flat_properties, flatten

settings_app = typer.Typer(
    help="Get or change log-master's own settings (everything but sources).", no_args_is_help=True
)


def register(app: typer.Typer) -> None:
    """Add the `settings` group to `app`."""
    app.add_typer(settings_app, name='settings')


@settings_app.command('get')
def settings_get(ctx: typer.Context, as_json: JsonOpt = False) -> None:
    """Show all settings."""
    api, as_json = setup(ctx, as_json)
    document = api.get('/settings')
    if as_json:
        print_json(document)
        return
    properties = flat_properties(document['schema'])
    table = Table(show_edge=False, pad_edge=False)
    for column in ('Key', 'Value', 'Notes'):
        table.add_column(column)
    for key, value in flatten(document['values']).items():
        notes = 'restart required' if properties.get(key, {}).get('x-restart-required') else ''
        table.add_row(key, str(value), notes)
    console.print(table)


@settings_app.command('set')
def settings_set(
    ctx: typer.Context,
    assignments: Annotated[
        list[str], typer.Argument(help='key=value pairs with dotted keys, e.g. poll.interval_s=0.5.')
    ],
    as_json: JsonOpt = False,
) -> None:
    """Change settings; they apply live except http.bind and state_file."""
    api, as_json = setup(ctx, as_json)
    document = api.get('/settings')
    try:
        patch = build_patch(document['schema'], assignments)
    except ValueError as exc:
        raise CliError(str(exc), EXIT_USAGE)
    result = api.request('PATCH', '/settings', json=patch)
    if as_json:
        print_json(result)
        return
    console.print(f'Updated {", ".join(flatten(patch))}')
    properties = flat_properties(result['schema'])
    restart = [key for key in flatten(patch) if properties.get(key, {}).get('x-restart-required')]
    if restart:
        console.print(f'[yellow]! restart log-master to apply {", ".join(restart)}[/]')
