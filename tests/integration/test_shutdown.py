"""Shutdown of logserver-fake on SIGINT/SIGTERM."""

import os
import signal

import pytest

from .conftest import wait_until

pytestmark = pytest.mark.integration

STORMS = 5
SIGNALS_PER_STORM = 200


@pytest.mark.parametrize('storm', range(STORMS))
def test_signal_storm_still_finalizes_the_log(make_fake, storm: int) -> None:
    """Bursts of SIGINT/SIGTERM (which make Python run handlers nested) still stop the server cleanly.

    A handler that takes a lock (e.g. `threading.Event.set()`) deadlocks in a fraction of such
    storms, so this test repeats them.
    """
    fake = make_fake('rover')
    log_id = fake.post('/log/start', {}).json()['log_id']
    wait_until(lambda: fake.recording()['actual_start_unix_ns'] is not None, message='first unit')
    for _ in range(SIGNALS_PER_STORM):
        if fake.proc.poll() is not None:
            break
        os.kill(fake.proc.pid, signal.SIGINT)
        os.kill(fake.proc.pid, signal.SIGTERM)
    assert fake.proc.wait(timeout=10) == 0, fake.output()
    manifest = fake.manifest(log_id)
    assert manifest['state'] == 'complete'
    assert manifest['segments'][0]['ended_cleanly']
