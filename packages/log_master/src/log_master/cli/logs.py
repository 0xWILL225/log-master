"""`log-master logs` and `log-master download`."""

import json
import os
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

import httpx
import typer
from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TransferSpeedColumn
from rich.table import Table

from log_master.cli.client import EXIT_USAGE, Api, CliError, raise_for_error
from log_master.cli.common import JsonOpt, console, setup, split_csv
from log_master.cli.format import format_bytes, format_duration_ns, format_time_ns, parse_time, print_json

DOWNLOAD_STATE_NAME = '.log-master-download.json'
_CHUNK_SIZE = 256 * 1024

logs_app = typer.Typer(help='List and inspect logs.')


def register(app: typer.Typer) -> None:
    """Add the `logs` group and the `download` command to `app`."""
    app.add_typer(logs_app, name='logs')
    app.command()(download)


def _part_label(part: dict[str, Any]) -> str:
    style = {'complete': 'green', 'interrupted': 'yellow', 'recording': 'red'}[part['state']]
    gap = ' ⧖' if len(part['segments']) > 1 else ''
    return f'[{style}]{part["source"]}{gap}[/]'


def _flags(log: dict[str, Any]) -> str:
    flags = [name for name in ('recording', 'incomplete', 'standalone', 'gaps') if log[name]]
    return ', '.join(flags)


@logs_app.callback(invoke_without_command=True)
def logs(
    ctx: typer.Context,
    since: Annotated[str | None, typer.Option('--since', help='ISO date/time (local), e.g. 2026-10-17.')] = None,
    until: Annotated[str | None, typer.Option('--until', help='ISO date/time (local).')] = None,
    source: Annotated[str | None, typer.Option('--source', help='Only logs with a part from this source.')] = None,
    as_json: JsonOpt = False,
) -> None:
    """List logs, newest first."""
    if ctx.invoked_subcommand is not None:
        return
    api, as_json = setup(ctx, as_json)
    params: dict[str, Any] = {}
    try:
        if since:
            params['since'] = parse_time(since)
        if until:
            params['until'] = parse_time(until)
    except ValueError as exc:
        raise CliError(str(exc), EXIT_USAGE)
    if source:
        params['source'] = source
    result = api.get('/logs', params=params)
    if as_json:
        print_json(result)
        return
    table = Table(show_edge=False, pad_edge=False)
    table.add_column('Log ID', no_wrap=True)
    table.add_column('Start', no_wrap=True)
    for column in ('Duration', 'Sources', 'Size', 'Flags'):
        table.add_column(column)
    for log in result['logs']:
        table.add_row(
            log['log_id'],
            format_time_ns(log['start_unix_ns']),
            format_duration_ns(log['duration_ns']),
            ' '.join(_part_label(part) for part in log['parts']),
            format_bytes(log['total_bytes']),
            _flags(log),
        )
    console.print(table)
    if result['unreachable_sources']:
        console.print(f'[yellow]! unreachable, logs not listed: {", ".join(result["unreachable_sources"])}[/]')


@logs_app.command('show')
def show(ctx: typer.Context, log_id: str, as_json: JsonOpt = False) -> None:
    """Show one log with its per-source segments and files."""
    api, as_json = setup(ctx, as_json)
    result = api.get(f'/logs/{log_id}')
    if as_json:
        print_json(result)
        return
    log = result['log']
    console.print(
        f'[bold]{log["log_id"]}[/]  {format_time_ns(log["start_unix_ns"])}  {format_duration_ns(log["duration_ns"])}  {format_bytes(log["total_bytes"])}  {_flags(log)}'
    )
    if log['missing_sources']:
        console.print(f'[yellow]! no part from: {", ".join(log["missing_sources"])}[/]')
    for part in log['parts']:
        console.print(f'\n{_part_label(part)}  {part["state"]}  origin {part["origin"]}')
        previous_end = None
        for segment in part['segments']:
            if previous_end is not None and segment['start_unix_ns'] is not None:
                console.print(f'    [yellow]gap {format_duration_ns(segment["start_unix_ns"] - previous_end)}[/]')
            clean = '' if segment['ended_cleanly'] else ' [yellow](unclean end)[/]'
            console.print(
                f'  segment {segment["index"]}: {format_time_ns(segment["start_unix_ns"])} → {format_time_ns(segment["end_unix_ns"])}{clean}'
            )
            previous_end = segment['end_unix_ns']
        for entry in part['files']:
            console.print(f'    {entry["path"]:<40} {format_bytes(entry["size"]):>10}')
    if result['unreachable_sources']:
        console.print(f'[yellow]! unreachable: {", ".join(result["unreachable_sources"])}[/]')


def _load_download_state(directory: Path) -> dict[str, dict[str, Any]]:
    path = directory / DOWNLOAD_STATE_NAME
    return json.loads(path.read_text()) if path.exists() else {}


def _save_download_state(directory: Path, state: dict[str, dict[str, Any]]) -> None:
    path = directory / DOWNLOAD_STATE_NAME
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, path)


def download_file(
    api: Api, log_id: str, source: str, entry: dict[str, Any], directory: Path, progress: Progress | None
) -> str:
    """Download one file into `directory`, resuming a previous partial download when possible.

    The ETag received with every download is remembered in `.log-master-download.json`. A file
    is skipped when it exists with the listed size and the recorded size/mtime/ETag still match;
    a partial `.part` file is resumed with `Range` + `If-Range: <ETag>`.

    Returns:
        `skipped`, `resumed` or `downloaded`.
    """
    path = entry['path']
    final = directory / path
    part = final.with_name(final.name + '.part')
    final.parent.mkdir(parents=True, exist_ok=True)
    state = _load_download_state(directory)
    record = state.get(path)
    unchanged = (
        record is not None
        and record.get('size') == entry['size']
        and record.get('mtime_unix_ns') == entry['mtime_unix_ns']
    )
    if unchanged and record.get('complete') and final.exists() and final.stat().st_size == entry['size']:
        return 'skipped'

    offset = part.stat().st_size if unchanged and record.get('etag') and part.exists() else 0
    if offset > entry['size']:
        offset = 0
    headers = {'Range': f'bytes={offset}-', 'If-Range': record['etag']} if 0 < offset < entry['size'] else {}
    outcome = 'resumed' if offset else 'downloaded'
    task = progress.add_task(f'{source}/{path}', total=entry['size'], completed=offset) if progress else None
    if offset == 0 or headers:
        url = f'/logs/{quote(log_id)}/sources/{quote(source)}/files/{quote(path, safe="/")}'
        try:
            with api.http.stream('GET', url, headers=headers, timeout=httpx.Timeout(30.0, read=60.0)) as response:
                if response.status_code >= 400:
                    response.read()
                    raise_for_error(response)
                if response.status_code != 206:
                    outcome = 'downloaded'
                    if task is not None and progress is not None:
                        progress.update(task, completed=0)
                state[path] = {
                    'etag': response.headers.get('etag'),
                    'size': entry['size'],
                    'mtime_unix_ns': entry['mtime_unix_ns'],
                    'complete': False,
                }
                _save_download_state(directory, state)
                with open(part, 'ab' if response.status_code == 206 else 'wb') as file:
                    for chunk in response.iter_raw(_CHUNK_SIZE):
                        file.write(chunk)
                        if task is not None and progress is not None:
                            progress.advance(task, len(chunk))
        except httpx.TransportError as exc:
            raise CliError(f'download of {source}/{path} interrupted ({exc}); run the command again to resume')
    if part.stat().st_size != entry['size']:
        raise CliError(
            f'{source}/{path}: expected {entry["size"]} bytes, got {part.stat().st_size}; run again to resume'
        )
    os.replace(part, final)
    state[path]['complete'] = True
    _save_download_state(directory, state)
    return outcome


def _download_zip(api: Api, log_id: str, sources: list[str], output: Path, as_json: bool) -> None:
    target = output / f'{log_id}.zip'
    part = target.with_name(target.name + '.part')
    params = {'sources': ','.join(sources)} if sources else {}
    output.mkdir(parents=True, exist_ok=True)
    try:
        with api.http.stream(
            'GET', f'/logs/{quote(log_id)}/download.zip', params=params, timeout=httpx.Timeout(30.0, read=60.0)
        ) as response:
            if response.status_code >= 400:
                response.read()
                raise_for_error(response)
            total = int(response.headers.get('content-length', 0)) or None
            with _progress(as_json) as progress, open(part, 'wb') as file:
                task = progress.add_task(target.name, total=total)
                for chunk in response.iter_raw(_CHUNK_SIZE):
                    file.write(chunk)
                    progress.advance(task, len(chunk))
    except httpx.TransportError as exc:
        raise CliError(f'zip download interrupted ({exc}); zip downloads cannot be resumed, use per-file downloads')
    if total is not None and part.stat().st_size != total:
        raise CliError(f'zip download incomplete: expected {total} bytes, got {part.stat().st_size}')
    os.replace(part, target)
    if as_json:
        print_json({'zip': str(target), 'bytes': target.stat().st_size})
    else:
        console.print(f'Saved {target} ({format_bytes(target.stat().st_size)})')


def _progress(disabled: bool) -> Progress:
    return Progress(
        TextColumn('{task.description}'),
        BarColumn(),
        DownloadColumn(binary_units=True),
        TransferSpeedColumn(),
        console=console,
        disable=disabled,
        transient=False,
    )


def download(
    ctx: typer.Context,
    log_id: str,
    sources: Annotated[
        list[str] | None, typer.Option('-s', '--sources', help='Only these sources (comma-separated).')
    ] = None,
    output: Annotated[Path, typer.Option('-o', '--output', help='Output directory.')] = Path('.'),
    as_zip: Annotated[
        bool, typer.Option('--zip', help='Download one streaming zip instead of individual files.')
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """Download a log into OUTPUT/<log_id>/<source>/ (resumable), or as one zip with --zip."""
    api, as_json = setup(ctx, as_json)
    names = split_csv(sources)
    if as_zip:
        _download_zip(api, log_id, names, output, as_json)
        return
    log = api.get(f'/logs/{log_id}')['log']
    parts = [part for part in log['parts'] if not names or part['source'] in names]
    missing = sorted(set(names) - {part['source'] for part in parts})
    if missing:
        raise CliError(f'no part of log {log_id} from: {", ".join(missing)}')
    recording = [part['source'] for part in parts if part['state'] == 'recording']
    if recording:
        raise CliError(f'log {log_id} is still being recorded by {", ".join(recording)}')
    results = []
    with _progress(as_json) as progress:
        for part in parts:
            directory = output / log_id / part['source']
            for entry in part['files']:
                outcome = download_file(api, log_id, part['source'], entry, directory, progress)
                results.append(
                    {'source': part['source'], 'path': entry['path'], 'size': entry['size'], 'result': outcome}
                )
    if as_json:
        print_json({'directory': str(output / log_id), 'files': results})
        return
    counts = {key: sum(1 for r in results if r['result'] == key) for key in ('downloaded', 'resumed', 'skipped')}
    console.print(
        f'{output / log_id}: {counts["downloaded"]} downloaded, {counts["resumed"]} resumed, {counts["skipped"]} already complete'
    )
