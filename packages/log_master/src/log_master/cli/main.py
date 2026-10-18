"""Entry point of the `log-master` CLI: status, recording control and `serve`."""

import logging
import sys
import uuid
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from log_master.cli import logs as logs_commands
from log_master.cli import settings as settings_commands
from log_master.cli import sources as sources_commands
from log_master.cli.client import DEFAULT_URL, EXIT_CONFLICT, EXIT_USAGE, ApiCallError, CliError
from log_master.cli.common import GlobalOptions, JsonOpt, console, setup, split_csv, styled
from log_master.cli.format import format_bytes, format_duration_ns, format_time_ns, print_json
from log_master.config import DEFAULT_CONFIG_PATH

# In-flight requests (e.g. a zip download or a proxied call to an unresponsive source) are cut off
# this long after SIGINT/SIGTERM, so Ctrl-C and `systemctl stop` return promptly.
GRACEFUL_SHUTDOWN_S = 3

app = typer.Typer(
    name='log-master',
    help='Coordinate synchronized recording across logging servers and download the logs.',
    no_args_is_help=True,
    add_completion=False,
)
pending_app = typer.Typer(help='Manage stops that sources have not confirmed.', no_args_is_help=True)
app.add_typer(pending_app, name='pending')
logs_commands.register(app)
sources_commands.register(app)
settings_commands.register(app)


@app.callback()
def root(
    ctx: typer.Context,
    url: Annotated[str, typer.Option('--url', envvar='LOG_MASTER_URL', help='log-master base URL.')] = DEFAULT_URL,
    as_json: JsonOpt = False,
) -> None:
    """Coordinate synchronized recording across logging servers and download the logs."""
    ctx.obj = GlobalOptions(url=url, json=as_json)


def render_state(state: dict[str, Any]) -> None:
    """Print the dashboard: recording header, source table and warnings."""
    now = state['server_time_unix_ns']
    active = state['active_log']
    if active is None:
        console.print('[bold]IDLE[/]')
    else:
        scheduled = active['scheduled_start_unix_ns']
        actual = active['actual_start_unix_ns']
        if actual is None and now < scheduled:
            console.print(f'[bold yellow]ARMING[/] {active["log_id"]} — starts in {(scheduled - now) / 1e9:.1f} s')
        else:
            elapsed = format_duration_ns(now - (actual or scheduled))
            line = f'[bold red]RECORDING[/] {active["log_id"]} — elapsed {elapsed}'
            if active['stop_at_unix_ns'] is not None:
                line += f', remaining {format_duration_ns(max(0, active["stop_at_unix_ns"] - now))}'
            console.print(line)

    table = Table(show_edge=False, pad_edge=False)
    table.add_column('Source', no_wrap=True)
    for column in ('Online', 'Health', 'Log', 'Written', 'Disk free', 'Details'):
        table.add_column(column)
    for source in state['sources']:
        status = source['status'] or {}
        health = status.get('health') or {}
        disk = status.get('disk')
        recording = status.get('recording') or {}
        online = '[green]yes[/]' if source['online'] else f'[red]no[/] ({_age(now, source["last_seen_unix_ns"])})'
        health_text = styled(health.get('level', '-'), health.get('level'))
        if health.get('message'):
            health_text += f' {escape(health["message"])}'
        details = escape(
            ', '.join(
                f'{d["label"]}: {d["value"]}{" " + d["unit"] if d.get("unit") else ""}'
                for d in status.get('details') or []
            )
        )
        disk_text = f'{format_bytes(disk["free_bytes"])}' if disk else '-'
        if source['low_disk']:
            disk_text = f'[yellow]{disk_text}[/]'
        table.add_row(
            source['name'],
            online,
            health_text,
            source['log_status'] or recording.get('state', '-'),
            format_bytes(recording.get('bytes_written')) if recording.get('log_id') else '-',
            disk_text,
            details,
        )
    console.print(table)
    for warning in state['warnings']:
        style = 'bold red' if warning['level'] == 'error' else 'yellow'
        console.print(f'[{style}]! {escape(warning["message"])}[/]')


def _age(now: int, last_seen: int | None) -> str:
    if last_seen is None:
        return 'never seen'
    return f'last seen {format_duration_ns(now - last_seen)} ago'


@app.command()
def status(ctx: typer.Context, as_json: JsonOpt = False) -> None:
    """Show the dashboard: recording state, sources and warnings."""
    api, as_json = setup(ctx, as_json)
    state = api.get('/state')
    if as_json:
        print_json(state)
    else:
        render_state(state)


@app.command()
def start(
    ctx: typer.Context,
    sources: Annotated[
        list[str] | None,
        typer.Option('-s', '--sources', help='Sources to record (comma-separated). Default: enabled ones.'),
    ] = None,
    exclude: Annotated[list[str] | None, typer.Option('--exclude', help='Default sources to leave out.')] = None,
    duration: Annotated[
        float | None, typer.Option('--duration', min=0.001, help='Stop automatically after N seconds.')
    ] = None,
    force: Annotated[bool, typer.Option('--force', help='Stop busy sources and include them.')] = False,
    skip_busy: Annotated[bool, typer.Option('--skip-busy', help='Leave busy sources out of the log.')] = False,
    as_json: JsonOpt = False,
) -> None:
    """Start a synchronized log."""
    api, as_json = setup(ctx, as_json)
    if sources and exclude:
        raise CliError('use either --sources or --exclude', EXIT_USAGE)
    if force and skip_busy:
        raise CliError('use either --force or --skip-busy', EXIT_USAGE)
    names: list[str] | None = split_csv(sources) if sources else None
    if exclude:
        excluded = split_csv(exclude)
        configured = api.get('/sources')['sources']
        unknown = sorted(set(excluded) - {s['name'] for s in configured})
        if unknown:
            raise CliError(f'unknown sources: {", ".join(unknown)}', EXIT_USAGE)
        names = [s['name'] for s in configured if s['enabled_by_default'] and s['name'] not in excluded]
    body: dict[str, Any] = {
        'request_id': uuid.uuid4().hex,
        'sources': names,
        'duration_s': duration,
        'skip_busy': skip_busy,
    }
    try:
        result = api.post('/recording/start', json=body)
    except ApiCallError as exc:
        if exc.code != 'conflicts':
            raise
        conflicts = exc.details.get('conflicts', [])
        if not force:
            lines = '\n'.join(f'  {c["source"]}: {c["origin"]} log {c["log_id"]}' for c in conflicts)
            raise CliError(
                f'sources are busy with other logs:\n{lines}\n'
                'use --force to stop and include them, or --skip-busy to leave them out',
                EXIT_CONFLICT,
            )
        body = {**body, 'request_id': uuid.uuid4().hex, 'takeover': [c['source'] for c in conflicts]}
        result = api.post('/recording/start', json=body)
    if as_json:
        print_json(result)
        return
    active = result['active_log']
    console.print(f'Started log [bold]{active["log_id"]}[/] at {format_time_ns(active["scheduled_start_unix_ns"])}')
    for participant in active['participants']:
        reason = f' ({participant["reason"]})' if participant['reason'] else ''
        console.print(f'  {participant["name"]}: {participant["status"]}{reason}')
    if active['stop_at_unix_ns'] is not None:
        console.print(f'Stops automatically at {format_time_ns(active["stop_at_unix_ns"])}')
    for warning in result['warnings']:
        console.print(f'[yellow]! {escape(warning["message"])}[/]')


@app.command()
def stop(ctx: typer.Context, as_json: JsonOpt = False) -> None:
    """Stop the active log."""
    api, as_json = setup(ctx, as_json)
    result = api.post('/recording/stop')
    if as_json:
        print_json(result)
        return
    console.print(f'Stopping log [bold]{result["log_id"]}[/] at {format_time_ns(result["stop_at_unix_ns"])}')
    if result['stop_pending']:
        console.print(
            f'[yellow]! stop not confirmed by {", ".join(result["stop_pending"])}; retrying in the background[/]'
        )


@pending_app.command('dismiss')
def pending_dismiss(ctx: typer.Context, source: str, as_json: JsonOpt = False) -> None:
    """Forget the pending stop of SOURCE."""
    api, as_json = setup(ctx, as_json)
    api.post(f'/recording/dismiss-pending/{source}')
    if as_json:
        print_json({'dismissed': source})
    else:
        console.print(f'Pending stop of {source} dismissed')


@app.command()
def serve(
    config: Annotated[Path, typer.Option('--config', help='Path of config.yaml.')] = DEFAULT_CONFIG_PATH,
    log_level: Annotated[str, typer.Option('--log-level', help='Python log level.')] = 'info',
    access_log: Annotated[bool, typer.Option('--access-log', help='Log every HTTP request.')] = False,
) -> None:
    """Run the log-master daemon (used by systemd)."""
    import uvicorn

    from log_master.app import create_app
    from log_master.config import ConfigFile, parse_bind

    logging.basicConfig(level=log_level.upper(), format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    host, port = parse_bind(ConfigFile(config).config.http.bind)
    uvicorn.run(
        create_app(config),
        host=host,
        port=port,
        log_level=log_level,
        access_log=access_log,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
    )


def main() -> None:
    """Run the CLI; :class:`CliError` is reported without traceback and sets the exit code."""
    try:
        app()
    except CliError as exc:
        Console(stderr=True, highlight=False).print(f'[bold red]Error:[/] {escape(exc.message)}')
        sys.exit(exc.exit_code)


if __name__ == '__main__':
    main()
