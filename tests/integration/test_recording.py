"""Integration tests of the recording lifecycle across several logging servers (spec §13)."""

import time

import pytest

from .conftest import wait_until

pytestmark = pytest.mark.integration

START_TOLERANCE_MS = 150


def test_synchronized_start_and_stop(make_fake, make_log_master) -> None:
    """Sources start within tolerance of the scheduled time and stop at the same instant."""
    rover, base = make_fake('rover'), make_fake('base')
    lm = make_log_master([rover, base])
    response = lm.start_log()
    assert response.status_code == 200, response.text
    active = response.json()['active_log']
    log_id = active['log_id']
    assert {p['name']: p['status'] for p in active['participants']} == {'rover': 'armed', 'base': 'armed'}
    assert lm.state()['state'] == 'recording'

    for name in ('rover', 'base'):
        lm.wait_participant(name, 'recording')
        wait_until(lambda n=name: lm.participant(n)['actual_start_unix_ns'] is not None, message='first unit')
    for name in ('rover', 'base'):
        assert lm.participant(name)['late_start_ms'] < START_TOLERANCE_MS
    assert not [w for w in lm.state()['warnings'] if w['code'] == 'late_start']

    time.sleep(0.5)
    stop = lm.post('/recording/stop')
    assert stop.status_code == 200 and stop.json()['stop_pending'] == []
    assert lm.state()['state'] == 'idle'
    stop_at = stop.json()['stop_at_unix_ns']
    wait_until(lambda: rover.recording()['state'] == 'idle' and base.recording()['state'] == 'idle', message='idle')

    starts = []
    for fake in (rover, base):
        manifest = fake.manifest(log_id)
        assert manifest['state'] == 'complete'
        assert manifest['origin'] == 'log-master'
        assert len(manifest['segments']) == 1
        segment = manifest['segments'][0]
        assert segment['ended_cleanly'] and segment['end_unix_ns'] == stop_at
        assert segment['start_unix_ns'] - manifest['scheduled_start_unix_ns'] < START_TOLERANCE_MS * 1e6
        starts.append(segment['start_unix_ns'])
    assert abs(starts[0] - starts[1]) < START_TOLERANCE_MS * 1e6

    log = lm.get(f'/logs/{log_id}').json()['log']
    assert [p['source'] for p in log['parts']] == ['base', 'rover']
    assert not (log['incomplete'] or log['recording'] or log['gaps'] or log['standalone'])
    assert lm.post('/recording/stop').json()['error']['code'] == 'not_recording'


def test_start_request_id_is_idempotent(make_fake, make_log_master) -> None:
    """Repeating a start with the same request_id returns the original response."""
    lm = make_log_master([make_fake('rover')])
    body = {'request_id': 'double-tap'}
    first = lm.post('/recording/start', body)
    second = lm.post('/recording/start', body)
    assert first.status_code == second.status_code == 200
    assert first.json()['active_log']['log_id'] == second.json()['active_log']['log_id']
    other = lm.start_log()
    assert other.status_code == 409 and other.json()['error']['code'] == 'already_recording'


def test_offline_source_late_joins_with_a_single_segment(make_fake, make_log_master) -> None:
    """A source offline at start joins when it comes online; its part has one segment."""
    rover = make_fake('rover')
    base = make_fake('base', start=False)
    lm = make_log_master([rover, base])
    response = lm.start_log()
    log_id = response.json()['active_log']['log_id']
    base_entry = next(p for p in response.json()['active_log']['participants'] if p['name'] == 'base')
    assert base_entry['status'] == 'not_joined'
    assert any(w['code'] == 'not_joined' and w['source'] == 'base' for w in response.json()['warnings'])

    lm.wait_participant('rover', 'recording')
    base.start()
    lm.wait_participant('base', 'recording')
    assert not [w for w in lm.state()['warnings'] if w['code'] == 'not_joined']
    lm.post('/recording/stop')
    wait_until(lambda: base.recording()['state'] == 'idle', message='base idle')
    manifest = base.manifest(log_id)
    assert manifest['state'] == 'complete'
    assert [s['index'] for s in manifest['segments']] == [0]
    assert manifest['segments'][0]['start_unix_ns'] > rover.manifest(log_id)['segments'][0]['start_unix_ns']


def test_crash_and_rejoin_creates_a_new_segment(make_fake, make_log_master) -> None:
    """A source that crashes mid-log rejoins as segment 1; the manifest shows the gap."""
    rover, base = make_fake('rover'), make_fake('base')
    lm = make_log_master([rover, base])
    log_id = lm.start_log().json()['active_log']['log_id']
    lm.wait_participant('base', 'recording')
    time.sleep(0.3)

    base.kill()
    lm.wait_participant('base', 'lost')
    assert any(w['code'] == 'lost' for w in lm.state()['warnings'])
    base.start()
    participant = lm.wait_participant('base', 'recording')
    assert participant['segment_index'] == 1
    time.sleep(0.3)
    lm.post('/recording/stop')
    wait_until(lambda: base.recording()['state'] == 'idle', message='base idle')

    manifest = base.manifest(log_id)
    assert manifest['state'] == 'interrupted'
    assert [s['index'] for s in manifest['segments']] == [0, 1]
    first, second = manifest['segments']
    assert not first['ended_cleanly'] and second['ended_cleanly']
    assert second['start_unix_ns'] > first['end_unix_ns']
    assert second['files'] == ['fake.001.txt', 'fake.001.timing.csv']
    log = lm.get(f'/logs/{log_id}').json()['log']
    assert log['gaps'] and log['incomplete']
    assert rover.manifest(log_id)['state'] == 'complete'


@pytest.mark.parametrize('keep_state_file', [True, False], ids=['resume', 'adopt'])
def test_log_master_restart_resumes_the_active_log(make_fake, make_log_master, keep_state_file) -> None:
    """Servers keep recording while log-master is down; a restarted log-master resumes/adopts and stops it."""
    rover, base = make_fake('rover'), make_fake('base')
    lm = make_log_master([rover, base])
    log_id = lm.start_log().json()['active_log']['log_id']
    lm.wait_participant('rover', 'recording')
    lm.wait_participant('base', 'recording')
    lm.kill()
    if not keep_state_file:
        lm.state_path.unlink()
    time.sleep(0.5)
    assert rover.recording()['log_id'] == log_id and rover.recording()['state'] == 'recording'

    lm.start()
    state = wait_until(lambda: (s := lm.state())['active_log'] is not None and s, message='resumed log')
    assert state['active_log']['log_id'] == log_id
    lm.wait_participant('rover', 'recording')
    lm.wait_participant('base', 'recording')
    assert lm.post('/recording/stop').status_code == 200
    wait_until(lambda: rover.recording()['state'] == base.recording()['state'] == 'idle', message='idle')
    assert rover.manifest(log_id)['state'] == base.manifest(log_id)['state'] == 'complete'


def test_pending_stop_resolves_when_the_source_returns(make_fake, make_log_master) -> None:
    """A stop that a source misses is retried; a new log can start meanwhile and the source joins it later."""
    rover, base = make_fake('rover'), make_fake('base')
    lm = make_log_master([rover, base])
    first_id = lm.start_log().json()['active_log']['log_id']
    lm.wait_participant('base', 'recording')

    assert base.post('/actions/go-offline', {'seconds': 3}).status_code == 200
    wait_until(lambda: not lm.source('base')['online'], message='base offline')
    stop = lm.post('/recording/stop').json()
    assert stop['stop_pending'] == ['base']
    state = lm.state()
    assert state['state'] == 'idle' and state['stop_pending'] == {'base': first_id}
    assert any(w['code'] == 'stop_pending' for w in state['warnings'])

    second = lm.start_log()
    assert second.status_code == 200, second.text
    second_id = second.json()['active_log']['log_id']
    base_entry = next(p for p in second.json()['active_log']['participants'] if p['name'] == 'base')
    assert base_entry['status'] == 'not_joined'

    lm.wait_participant('base', 'recording', timeout=15)
    assert lm.state()['stop_pending'] == {}
    assert base.manifest(first_id)['state'] == 'complete'
    assert base.recording()['log_id'] == second_id
    lm.post('/recording/stop')


def test_dismiss_pending_stop(make_fake, make_log_master) -> None:
    """A pending stop can be dismissed."""
    rover = make_fake('rover')
    lm = make_log_master([rover])
    lm.start_log()
    lm.wait_participant('rover', 'recording')
    rover.post('/actions/go-offline', {'seconds': 2})
    wait_until(lambda: not lm.source('rover')['online'], message='rover offline')
    assert lm.post('/recording/stop').json()['stop_pending'] == ['rover']
    assert lm.source('rover')['log_status'] == 'stop_pending'
    assert lm.post('/recording/dismiss-pending/rover').status_code == 204
    assert lm.state()['stop_pending'] == {}
    assert lm.post('/recording/dismiss-pending/rover').status_code == 404


def test_standalone_conflict_takeover_and_skip_busy(make_fake, make_log_master) -> None:
    """A standalone log is a conflict (409): it can be skipped or taken over."""
    rover, base = make_fake('rover'), make_fake('base')
    standalone_id = base.post('/log/start').json()['log_id']
    lm = make_log_master([rover, base])
    wait_until(lambda: lm.source('base')['status']['recording']['state'] == 'recording', message='base status')
    assert any(w['code'] == 'foreign_recording' for w in lm.state()['warnings'])

    conflict = lm.start_log()
    assert conflict.status_code == 409
    error = conflict.json()['error']
    assert error['code'] == 'conflicts'
    assert error['details']['conflicts'] == [{'source': 'base', 'log_id': standalone_id, 'origin': 'standalone'}]
    assert lm.state()['state'] == 'idle'
    cli = lm.cli('start')
    assert cli.returncode == 3, cli.stdout + cli.stderr

    skipped = lm.start_log(skip_busy=True)
    assert skipped.status_code == 200
    assert {p['name']: p['status'] for p in skipped.json()['active_log']['participants']}['base'] == 'busy'
    lm.post('/recording/stop')
    assert base.recording()['log_id'] == standalone_id

    taken = lm.start_log(takeover=['base'])
    assert taken.status_code == 200, taken.text
    lm.wait_participant('base', 'recording')
    assert base.manifest(standalone_id)['state'] == 'complete'
    lm.post('/recording/stop')


def test_timed_log_stops_without_log_master(make_fake, make_log_master) -> None:
    """Servers end a timed log by themselves even when log-master is down."""
    rover, base = make_fake('rover'), make_fake('base')
    lm = make_log_master([rover, base])
    response = lm.start_log(duration_s=1.0).json()
    log_id = response['active_log']['log_id']
    stop_at = response['active_log']['stop_at_unix_ns']
    lm.kill()
    wait_until(
        lambda: rover.recording()['state'] == base.recording()['state'] == 'idle', timeout=5, message='timed stop'
    )
    for fake in (rover, base):
        manifest = fake.manifest(log_id)
        assert manifest['state'] == 'complete'
        assert manifest['segments'][0]['end_unix_ns'] == stop_at


def test_timed_log_is_stopped_by_log_master(make_fake, make_log_master) -> None:
    """log-master itself returns to idle at the stop time of a timed log."""
    lm = make_log_master([make_fake('rover')])
    lm.start_log(duration_s=0.5)
    wait_until(lambda: lm.state()['state'] == 'idle', timeout=5, message='log-master idle')


def test_late_start_warning(make_fake, make_log_master) -> None:
    """A source that writes its first unit late is flagged."""
    rover = make_fake('rover', '--slow-start-ms', '800')
    lm = make_log_master([rover])
    lm.start_log()
    wait_until(
        lambda: any(w['code'] == 'late_start' and w['source'] == 'rover' for w in lm.state()['warnings']),
        message='late start warning',
    )
    lm.post('/recording/stop')


def test_write_failure_is_reported(make_fake, make_log_master) -> None:
    """A disk-full write error ends the segment uncleanly and shows a health error; stop still works."""
    rover = make_fake('rover')
    lm = make_log_master([rover])
    log_id = lm.start_log().json()['active_log']['log_id']
    lm.wait_participant('rover', 'recording')
    assert lm.post('/sources/rover/actions/disk-full', {}).status_code == 200
    wait_until(lambda: any(w['code'] == 'health_error' for w in lm.state()['warnings']), message='health error warning')
    assert lm.participant('rover')['status'] == 'recording'
    lm.post('/recording/stop')
    wait_until(lambda: rover.recording()['state'] == 'idle', message='idle')
    manifest = rover.manifest(log_id)
    assert manifest['state'] == 'interrupted' and not manifest['segments'][0]['ended_cleanly']
