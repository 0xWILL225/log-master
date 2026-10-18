"""`log-master sources ...` (source list) and `log-master source <name> ...` (one source)."""

from typing import Annotated, Any

import typer
from rich.markup import escape
from rich.table import Table

from log_master.cli.client import EXIT_USAGE, CliError
from log_master.cli.common import JsonOpt, console, setup, styled
from log_master.cli.format import format_bytes, format_time_ns, print_json
from log_master.cli.values import build_patch, describe_param, flat_properties, flatten, parse_action_args

sources_app = typer.Typer(help='List and edit the configured sources.')
source_app = typer.Typer(help='Inspect and control one source: status, config, actions.', no_args_is_help=True)
config_app = typer.Typer(help="Get or set the source's own configuration.", no_args_is_help=True)
source_app.add_typer(config_app, name='config')


def register(app: typer.Typer) -> None:
    """Add the `sources` and `source` groups to `app`."""
    app.add_typer(sources_app, name='sources')
    app.add_typer(source_app, name='source')


@sources_app.callback(invoke_without_command=True)
def sources(ctx: typer.Context, as_json: JsonOpt = False) -> None:
    """List configured sources."""
    if ctx.invoked_subcommand is not None:
        return
    api, as_json = setup(ctx, as_json)
    result = api.get('/sources')
    if as_json:
        print_json(result)
        return
    table = Table(show_edge=False, pad_edge=False)
    for column in ('Name', 'URL', 'Default', 'Online', 'Health', 'Type', 'Host'):
        table.add_column(column)
    for source in result['sources']:
        health = (source['status'] or {}).get('health') or {}
        info = source['info'] or {}
        table.add_row(
            source['name'],
            source['url'],
            'yes' if source['enabled_by_default'] else 'no',
            '[green]yes[/]' if source['online'] else '[red]no[/]',
            styled(health.get('level', '-'), health.get('level')),
            info.get('source_type', '-'),
            info.get('hostname', '-'),
        )
    console.print(table)


@sources_app.command('add')
def sources_add(
    ctx: typer.Context,
    name: str,
    url: str,
    no_default: Annotated[bool, typer.Option('--no-default', help='Do not record it by default.')] = False,
    as_json: JsonOpt = False,
) -> None:
    """Add a source NAME at URL."""
    api, as_json = setup(ctx, as_json)
    result = api.post('/sources', json={'name': name, 'url': url, 'enabled_by_default': not no_default})
    print_json(result) if as_json else console.print(f'Added source {name} ({url})')


@sources_app.command('edit')
def sources_edit(
    ctx: typer.Context,
    name: str,
    url: Annotated[str | None, typer.Option('--url', help='New base URL.')] = None,
    default: Annotated[bool | None, typer.Option('--default/--no-default', help='Record by default.')] = None,
    as_json: JsonOpt = False,
) -> None:
    """Change the URL or default selection of source NAME."""
    api, as_json = setup(ctx, as_json)
    body: dict[str, Any] = {}
    if url is not None:
        body['url'] = url
    if default is not None:
        body['enabled_by_default'] = default
    if not body:
        raise CliError('nothing to change (use --url and/or --default/--no-default)', EXIT_USAGE)
    result = api.request('PUT', f'/sources/{name}', json=body)
    print_json(result) if as_json else console.print(f'Updated source {name}')


@sources_app.command('remove')
def sources_remove(ctx: typer.Context, name: str, as_json: JsonOpt = False) -> None:
    """Remove source NAME."""
    api, as_json = setup(ctx, as_json)
    api.request('DELETE', f'/sources/{name}')
    print_json({'removed': name}) if as_json else console.print(f'Removed source {name}')


@source_app.callback()
def source(ctx: typer.Context, name: Annotated[str, typer.Argument(help='Source name.')]) -> None:
    """Inspect and control one source: status, config, actions."""
    ctx.meta['source'] = name


@source_app.command('status')
def source_status(ctx: typer.Context, as_json: JsonOpt = False) -> None:
    """Show info and full status of the source."""
    api, as_json = setup(ctx, as_json)
    name = ctx.meta['source']
    result = api.get(f'/sources/{name}')
    if as_json:
        print_json(result)
        return
    info = result['info'] or {}
    status = result['status'] or {}
    online = (
        '[green]online[/]'
        if result['online']
        else f'[red]offline[/] (last seen {format_time_ns(result["last_seen_unix_ns"])})'
    )
    console.print(f'[bold]{name}[/] {result["url"]} {online}')
    if info:
        console.print(
            f'  {info["source_type"]} {info["server_version"]} on {info["hostname"]}, logs in {info["log_directory"]}'
        )
    if result['error']:
        console.print(f'  [yellow]last error: {escape(result["error"])}[/]')
    if not status:
        return
    health = status['health']
    console.print(f'  health: {styled(health["level"], health["level"])} {escape(health["message"] or "")}')
    recording = status['recording']
    console.print(f'  recording: {recording["state"]} {recording["log_id"] or ""}')
    disk = status['disk']
    console.print(
        f'  disk: {format_bytes(disk["free_bytes"])} free of {format_bytes(disk["total_bytes"])} ({disk["path"]})'
    )
    table = Table(show_edge=False, pad_edge=False, show_header=False)
    table.add_column()
    table.add_column()
    for detail in status['details']:
        value = f'{detail["value"]}{" " + detail["unit"] if detail.get("unit") else ""}'
        table.add_row(escape(detail['label']), styled(value, detail.get('level')))
    console.print(table)


@config_app.command('get')
def config_get(
    ctx: typer.Context,
    key: Annotated[str | None, typer.Argument(help='Dotted key to show.')] = None,
    as_json: JsonOpt = False,
) -> None:
    """Show the source's config (or one KEY)."""
    api, as_json = setup(ctx, as_json)
    document = api.get(f'/sources/{ctx.meta["source"]}/config')
    values = flatten(document['values'])
    if key is not None:
        if key not in values:
            raise CliError(f'unknown key {key!r}', EXIT_USAGE)
        print_json(values[key]) if as_json else console.print(str(values[key]))
        return
    if as_json:
        print_json(document)
        return
    properties = flat_properties(document['schema'])
    table = Table(show_edge=False, pad_edge=False)
    for column in ('Key', 'Value', 'Notes'):
        table.add_column(column)
    for name, value in values.items():
        prop = properties.get(name, {})
        notes = [
            label
            for flag, label in (
                ('readOnly', 'read-only'),
                ('writeOnly', 'secret'),
                ('x-restart-required', 'restart required'),
            )
            if prop.get(flag)
        ]
        table.add_row(name, '***' if prop.get('writeOnly') else str(value), ', '.join(notes))
    console.print(table)


@config_app.command('set')
def config_set(
    ctx: typer.Context,
    assignments: Annotated[list[str], typer.Argument(help='key=value pairs; values are parsed per schema type.')],
    as_json: JsonOpt = False,
) -> None:
    """Change config values of the source (only while it is not recording)."""
    api, as_json = setup(ctx, as_json)
    name = ctx.meta['source']
    document = api.get(f'/sources/{name}/config')
    try:
        patch = build_patch(document['schema'], assignments)
    except ValueError as exc:
        raise CliError(str(exc), EXIT_USAGE)
    result = api.request('PATCH', f'/sources/{name}/config', json=patch)
    print_json(result) if as_json else console.print(f'Updated {", ".join(flatten(patch))} on {name}')


@source_app.command('actions')
def actions(ctx: typer.Context, as_json: JsonOpt = False) -> None:
    """List the source's actions with their parameters."""
    api, as_json = setup(ctx, as_json)
    result = api.get(f'/sources/{ctx.meta["source"]}/actions')
    if as_json:
        print_json(result)
        return
    table = Table(show_edge=False, pad_edge=False)
    for column in ('Action', 'Description', 'Parameters'):
        table.add_column(column)
    for action in result['actions']:
        params = '\n'.join(describe_param(n, p) for n, p in action['params_schema'].get('properties', {}).items())
        description = action['description'] or action['label']
        if not action['available']:
            description += f' [yellow](unavailable: {action["unavailable_reason"]})[/]'
        if action['confirm']:
            description += ' [dim](needs --yes)[/]'
        table.add_row(action['name'], description, params or '-')
    console.print(table)


@source_app.command('action', context_settings={'allow_extra_args': True, 'ignore_unknown_options': True})
def action(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help='Action name.')],
    yes: Annotated[bool, typer.Option('--yes', help='Confirm actions that ask for confirmation.')] = False,
    as_json: JsonOpt = False,
) -> None:
    """Run an action; parameters are given as --param value (see `actions`)."""
    api, as_json = setup(ctx, as_json)
    source_name = ctx.meta['source']
    specs = api.get(f'/sources/{source_name}/actions')['actions']
    spec = next((a for a in specs if a['name'] == name), None)
    if spec is None:
        raise CliError(
            f'no action {name!r} on {source_name} (available: {", ".join(a["name"] for a in specs)})', EXIT_USAGE
        )
    try:
        params = parse_action_args(spec['params_schema'], list(ctx.args))
    except ValueError as exc:
        raise CliError(str(exc), EXIT_USAGE)
    if spec['confirm'] and not yes:
        raise CliError(f'{spec["confirm"]}\npass --yes to confirm', EXIT_USAGE)
    result = api.post(f'/sources/{source_name}/actions/{name}', json=params)
    if as_json:
        print_json(result)
    else:
        console.print(result['message'])
        if result.get('data'):
            print_json(result['data'])
