"""Integration tests of downloads (Range, zip), conflict responses and the CLI."""

import io
import json
import time
import zipfile
from pathlib import Path

import httpx
import pytest

from .conftest import wait_until

pytestmark = pytest.mark.integration


def record(lm, fakes, seconds: float = 0.6) -> str:
    """Record a short complete log on all fakes and return its ID."""
    log_id = lm.start_log().json()['active_log']['log_id']
    for fake in fakes:
        lm.wait_participant(fake.name, 'recording')
    time.sleep(seconds)
    lm.post('/recording/stop')
    wait_until(lambda: all(f.recording()['state'] == 'idle' for f in fakes), message='sources idle')
    return log_id


@pytest.fixture
def recorded(make_fake, make_log_master):
    """Two fakes and log-master with one complete log."""
    rover, base = make_fake('rover'), make_fake('base')
    lm = make_log_master([rover, base])
    return lm, rover, base, record(lm, [rover, base])


def test_single_file_download_supports_range(recorded) -> None:
    """Proxied downloads forward Range/If-Range and set Content-Disposition."""
    lm, rover, _, log_id = recorded
    original = (rover.dir / log_id / 'fake.000.txt').read_bytes()
    url = f'/logs/{log_id}/sources/rover/files/fake.000.txt'
    full = lm.get(url)
    assert full.status_code == 200 and full.content == original
    assert full.headers['content-length'] == str(len(original))
    assert full.headers['content-disposition'] == f'attachment; filename="{log_id}_rover_fake.000.txt"'
    assert full.headers['accept-ranges'] == 'bytes'
    etag = full.headers['etag']
    resumed = lm.get(url, headers={'Range': 'bytes=1000-', 'If-Range': etag})
    assert resumed.status_code == 206
    assert resumed.content == original[1000:]
    assert resumed.headers['content-range'] == f'bytes 1000-{len(original) - 1}/{len(original)}'
    changed = lm.get(url, headers={'Range': 'bytes=1000-', 'If-Range': '"other"'})
    assert changed.status_code == 200 and changed.content == original


def test_zip_contents_and_sizes_match_the_sources(recorded) -> None:
    """The streamed zip has the documented layout, a correct Content-Length and the original bytes."""
    lm, rover, base, log_id = recorded
    response = lm.get(f'/logs/{log_id}/download.zip')
    assert response.status_code == 200
    assert response.headers['content-disposition'] == f'attachment; filename="{log_id}.zip"'
    assert int(response.headers['content-length']) == len(response.content)
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert archive.testzip() is None
    log = lm.get(f'/logs/{log_id}').json()['log']
    expected = {f'{log_id}/{p["source"]}/{f["path"]}': f['size'] for p in log['parts'] for f in p['files']}
    assert {i.filename: i.file_size for i in archive.infolist()} == expected
    assert all(i.compress_type == zipfile.ZIP_STORED for i in archive.infolist())
    for fake in (rover, base):
        for name in ('fake.000.txt', 'fake.000.timing.csv', 'manifest.json'):
            assert archive.read(f'{log_id}/{fake.name}/{name}') == (fake.dir / log_id / name).read_bytes()

    subset = zipfile.ZipFile(io.BytesIO(lm.get(f'/logs/{log_id}/download.zip', params={'sources': 'base'}).content))
    assert {name.split('/')[1] for name in subset.namelist()} == {'base'}


def test_logs_listing_and_filters(recorded) -> None:
    """The merged listing supports time and source filters and reports unreachable sources."""
    lm, rover, base, log_id = recorded
    listing = lm.get('/logs').json()
    assert [log['log_id'] for log in listing['logs']] == [log_id]
    assert listing['unreachable_sources'] == []
    start = listing['logs'][0]['start_unix_ns']
    assert lm.get('/logs', params={'since': start + 1}).json()['logs'] == []
    assert len(lm.get('/logs', params={'until': start + 1}).json()['logs']) == 1
    assert [p['source'] for p in lm.get('/logs', params={'source': 'rover'}).json()['logs'][0]['parts']] == ['rover']
    base.kill()
    wait_until(lambda: not lm.source('base')['online'], message='base offline')
    listing = lm.get('/logs').json()
    assert listing['unreachable_sources'] == ['base']
    assert [p['source'] for p in listing['logs'][0]['parts']] == ['rover']
    offline = lm.get(f'/logs/{log_id}/sources/base/files/manifest.json')
    assert offline.status_code == 503 and offline.json()['error']['code'] == 'source_offline'


def test_recording_log_cannot_be_downloaded_or_reconfigured(make_fake, make_log_master) -> None:
    """Downloads of a recording log and config patches while recording are 409."""
    rover = make_fake('rover')
    lm = make_log_master([rover])
    log_id = lm.start_log().json()['active_log']['log_id']
    lm.wait_participant('rover', 'recording')
    assert lm.get(f'/logs/{log_id}/download.zip').json()['error']['code'] == 'recording_in_progress'
    single = lm.get(f'/logs/{log_id}/sources/rover/files/manifest.json')
    assert single.status_code == 409 and single.json()['error']['code'] == 'recording_in_progress'
    patch = httpx.patch(f'{lm.url}/sources/rover/config', json={'rate_bytes_per_s': 1000})
    assert patch.status_code == 409 and patch.json()['error']['code'] == 'recording_in_progress'
    assert lm.get('/logs').json()['logs'][0]['recording'] is True
    remove = httpx.delete(f'{lm.url}/sources/rover')
    assert remove.status_code == 409 and remove.json()['error']['code'] == 'source_in_use'
    lm.post('/recording/stop')


def test_path_traversal_is_404(recorded) -> None:
    """Traversal attempts are rejected by the source and through the proxy."""
    lm, rover, _, log_id = recorded
    for path in ('..%2F..%2Fconfig.yaml', '%2E%2E%2Fconfig.yaml', '..%2F..%2F..%2F..%2Fetc%2Fpasswd'):
        assert httpx.get(f'{rover.url}/v1/logs/{log_id}/files/{path}').status_code == 404
        assert lm.get(f'/logs/{log_id}/sources/rover/files/{path}').status_code == 404


def test_proxied_config_and_actions(make_fake, make_log_master) -> None:
    """Config and actions are proxied unchanged, including error envelopes."""
    rover = make_fake('rover')
    lm = make_log_master([rover])
    document = lm.get('/sources/rover/config').json()
    assert document['schema']['properties']['name']['readOnly'] is True
    patched = httpx.patch(f'{lm.url}/sources/rover/config', json={'rate_bytes_per_s': 1234})
    assert patched.status_code == 200 and patched.json()['values']['rate_bytes_per_s'] == 1234
    invalid = httpx.patch(f'{lm.url}/sources/rover/config', json={'rate_bytes_per_s': 0})
    assert invalid.status_code == 422 and invalid.json()['error']['code'] == 'validation_error'
    actions = lm.get('/sources/rover/actions').json()['actions']
    assert 'survey-in' in [a['name'] for a in actions]
    result = lm.post('/sources/rover/actions/set-health', {'level': 'error', 'message': 'receiver unplugged'})
    assert result.status_code == 200
    wait_until(lambda: lm.source('rover')['status']['health']['level'] == 'error', message='health error')
    assert any(w['message'] == 'rover: receiver unplugged' for w in lm.state()['warnings'])
    assert lm.post('/sources/rover/actions/disk-full', {}).json()['error']['code'] == 'action_unavailable'
    assert lm.post('/sources/nope/actions/x', {}).status_code == 404


def test_source_list_editing(make_fake, make_log_master) -> None:
    """Sources can be added, edited and removed through the API; the poller follows."""
    rover, extra = make_fake('rover'), make_fake('extra')
    lm = make_log_master([rover])
    added = httpx.post(f'{lm.url}/sources', json={'name': 'extra', 'url': extra.url, 'enabled_by_default': False})
    assert added.status_code == 201
    wait_until(lambda: lm.source('extra')['online'], message='extra online')
    assert httpx.post(f'{lm.url}/sources', json={'name': 'extra', 'url': extra.url}).status_code == 409
    edited = httpx.put(f'{lm.url}/sources/extra', json={'enabled_by_default': True})
    assert edited.status_code == 200 and edited.json()['enabled_by_default'] is True
    started = lm.start_log()
    assert {p['name'] for p in started.json()['active_log']['participants']} == {'rover', 'extra'}
    assert httpx.put(f'{lm.url}/sources/extra', json={'name': 'renamed'}).json()['error']['code'] == 'source_in_use'
    assert httpx.put(f'{lm.url}/sources/extra', json={'url': extra.url + '/'}).status_code == 200
    lm.post('/recording/stop')
    assert httpx.delete(f'{lm.url}/sources/extra').status_code == 204
    assert [s['name'] for s in lm.get('/sources').json()['sources']] == ['rover']
    assert 'extra' not in lm.config_path.read_text()


def test_settings_api(make_fake, make_log_master) -> None:
    """Settings are validated, persisted and flag restart-required changes."""
    lm = make_log_master([make_fake('rover')])
    document = lm.get('/settings').json()
    assert 'sources' not in document['values']
    assert document['schema']['properties']['http']['properties']['bind']['x-restart-required'] is True
    ok = httpx.patch(f'{lm.url}/settings', json={'recording': {'late_start_warn_ms': 300}})
    assert ok.status_code == 200 and ok.json()['values']['recording']['late_start_warn_ms'] == 300
    assert httpx.patch(f'{lm.url}/settings', json={'poll': {'interval_s': -1}}).status_code == 422
    httpx.patch(f'{lm.url}/settings', json={'http': {'bind': '127.0.0.1:1'}})
    assert any(w['code'] == 'restart_required' for w in lm.state()['warnings'])


def test_cli_workflow(recorded, tmp_path: Path) -> None:
    """status/logs/download (resumable)/sources/source/settings via the CLI."""
    lm, rover, base, log_id = recorded
    status = lm.cli('status')
    assert status.returncode == 0 and 'IDLE' in status.stdout
    assert json.loads(lm.cli('--json', 'status').stdout)['state'] == 'idle'
    assert json.loads(lm.cli('status', '--json').stdout)['state'] == 'idle'
    logs = lm.cli('logs', '--source', 'rover')
    assert log_id in logs.stdout
    assert log_id in lm.cli('logs', 'show', log_id).stdout

    out = tmp_path / 'downloads'
    first = json.loads(lm.cli('download', log_id, '-o', str(out), '--json').stdout)
    assert {f['result'] for f in first['files']} == {'downloaded'}
    for fake in (rover, base):
        for name in ('fake.000.txt', 'manifest.json'):
            assert (out / log_id / fake.name / name).read_bytes() == (fake.dir / log_id / name).read_bytes()
    second = json.loads(lm.cli('download', log_id, '-o', str(out), '--json').stdout)
    assert {f['result'] for f in second['files']} == {'skipped'}

    target = out / log_id / 'rover' / 'fake.000.txt'
    original = target.read_bytes()
    state_path = target.parent / '.log-master-download.json'
    state = json.loads(state_path.read_text())
    state['fake.000.txt']['complete'] = False
    state_path.write_text(json.dumps(state))
    target.unlink()
    target.with_name('fake.000.txt.part').write_bytes(original[: len(original) // 2])
    third = json.loads(lm.cli('download', log_id, '-s', 'rover', '-o', str(out), '--json').stdout)
    results = {f['path']: f['result'] for f in third['files']}
    assert results['fake.000.txt'] == 'resumed'
    assert target.read_bytes() == original

    zipped = lm.cli('download', log_id, '-o', str(out), '--zip')
    assert zipped.returncode == 0, zipped.stderr
    assert zipfile.ZipFile(out / f'{log_id}.zip').testzip() is None

    assert 'rover' in lm.cli('sources').stdout
    assert lm.cli('sources', 'edit', 'rover', '--no-default').returncode == 0
    assert lm.source('rover')['enabled_by_default'] is False
    assert lm.cli('sources', 'edit', 'rover', '--default').returncode == 0

    assert 'survey-in' in lm.cli('source', 'rover', 'actions').stdout
    action = lm.cli('source', 'rover', 'action', 'survey-in', '--seconds', '300', '--accuracy-mm', '20', '--immediate')
    assert action.returncode == 0, action.stderr
    assert lm.cli('source', 'rover', 'action', 'crash').returncode == 2
    assert lm.cli('source', 'rover', 'action', 'survey-in', '--bogus', '1').returncode == 2
    assert lm.cli('source', 'rover', 'config', 'set', 'rate_bytes_per_s=5000', 'mode=binary').returncode == 0
    assert lm.cli('source', 'rover', 'config', 'get', 'rate_bytes_per_s').stdout.strip() == '5000'
    assert 'Fix' in lm.cli('source', 'rover', 'status').stdout

    assert lm.cli('settings', 'set', 'recording.late_start_warn_ms=250').returncode == 0
    assert json.loads(lm.cli('settings', 'get', '--json').stdout)['values']['recording']['late_start_warn_ms'] == 250
    assert lm.cli('settings', 'set', 'poll.nope=1').returncode == 2

    started = lm.cli('start', '--exclude', 'base', '--duration', '0.5')
    assert started.returncode == 0, started.stderr
    assert lm.participant('base') is None and lm.participant('rover') is not None
    wait_until(lambda: lm.state()['state'] == 'idle', message='timed stop')
    assert lm.cli('stop').returncode == 1
    assert lm.cli('pending', 'dismiss', 'rover').returncode == 1


def test_cli_unreachable_exit_code(tmp_path: Path) -> None:
    """Exit code 4 when log-master cannot be reached."""
    import subprocess
    import sys

    from .conftest import CHILD_ENV, free_port

    result = subprocess.run(
        [sys.executable, '-m', 'log_master.cli.main', '--url', f'http://127.0.0.1:{free_port()}', 'status'],
        capture_output=True,
        text=True,
        env=CHILD_ENV,
    )
    assert result.returncode == 4
    assert 'unreachable' in result.stderr
