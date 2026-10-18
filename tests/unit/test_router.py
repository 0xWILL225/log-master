"""Unit tests of the /v1 router: error envelopes, Range downloads, config and actions semantics."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from logserver.config import ConfigStore, config_field
from logserver.errors import ApiError
from logserver.models import ActionResult, ActionSpec, Detail, Health, Info, ManifestSource
from logserver.router import create_app
from logserver.session import LogSession
from pydantic import BaseModel, ConfigDict

LOG_ID = '2026-10-17T14-03-55Z_a1b2c3'


class SampleConfig(BaseModel):
    """Config with every annotation kind."""

    model_config = ConfigDict(extra='forbid')

    bind_address: str = config_field('0.0.0.0:1', read_only=True)
    rate: int = config_field(10, ge=1)
    port: str = config_field('/dev/ttyACM0', restart_required=True)
    password: str = config_field('hunter2', write_only=True)


class SampleBackend:
    """Minimal in-process backend."""

    def __init__(self, tmp_path: Path):
        self.info = Info(
            name='sample', source_type='unit', server_version='0', hostname='h', log_directory=str(tmp_path)
        )
        self.config = ConfigStore(SampleConfig, tmp_path / 'config.yaml')
        self.session = LogSession(
            tmp_path / 'logs', ManifestSource(name='sample', source_type='unit', server_version='0', hostname='h'), self
        )
        self.file = None
        self.ran: list[tuple[str, dict[str, Any]]] = []

    def open_segment(self, session: LogSession) -> None:
        """Open the segment's data file."""
        self.file = open(session.segment_path('data', '.bin'), 'ab')

    def close_segment(self, session: LogSession, clean: bool) -> None:
        """Close the segment's data file."""
        self.file.close()

    def health(self) -> Health:
        """Report healthy."""
        return Health()

    def details(self) -> list[Detail]:
        """Return one detail row."""
        return [Detail(key='k', label='K', value=1)]

    def actions(self) -> list[ActionSpec]:
        """Return one available and one unavailable action."""
        return [
            ActionSpec(
                name='survey-in',
                label='Survey-in',
                params_schema={'type': 'object', 'properties': {'seconds': {'type': 'integer', 'minimum': 1}}},
            ),
            ActionSpec(name='nope', label='Nope', available=False, unavailable_reason='not now'),
        ]

    def run_action(self, name: str, params: dict[str, Any]) -> ActionResult:
        """Record the call and echo the params."""
        self.ran.append((name, params))
        return ActionResult(message='ok', data=params)


@pytest.fixture
def backend(tmp_path: Path) -> SampleBackend:
    """A fresh sample backend."""
    return SampleBackend(tmp_path)


@pytest.fixture
def client(backend: SampleBackend) -> TestClient:
    """HTTP client for the sample backend."""
    return TestClient(create_app(backend))


def record_log(backend: SampleBackend, client: TestClient) -> None:
    """Record one complete log with ID `LOG_ID`."""
    record_log_with_id(backend, client, LOG_ID, b'payload')


def record_log_with_id(backend: SampleBackend, client: TestClient, log_id: str, payload: bytes) -> None:
    """Record one complete log containing `payload`."""
    assert client.post('/v1/log/start', json={'log_id': log_id}).status_code == 200
    now = backend.session.recording().scheduled_start_unix_ns
    assert backend.session.should_write(now)
    backend.file.write(payload)
    backend.session.on_written(len(payload), now)
    client.post('/v1/log/stop', json={})
    backend.session.tick(now + 10**12)


def test_info_and_status(client: TestClient) -> None:
    """Info and status follow the schema."""
    assert client.get('/v1/info').json()['protocol_version'] == 1
    status = client.get('/v1/status').json()
    assert status['recording'] == {
        'state': 'idle',
        'log_id': None,
        'origin': None,
        'scheduled_start_unix_ns': None,
        'actual_start_unix_ns': None,
        'scheduled_stop_unix_ns': None,
        'segment_index': None,
        'bytes_written': 0,
    }
    assert status['disk']['total_bytes'] > 0
    assert status['details'][0]['key'] == 'k'


def test_error_envelopes(client: TestClient) -> None:
    """Errors carry {error: {code, message, details}}."""
    assert client.post('/v1/log/start', json={'log_id': LOG_ID}).status_code == 200
    response = client.post('/v1/log/start', json={'log_id': '2026-10-17T15-00-00Z_000000'})
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'already_recording'
    assert response.json()['error']['details']['log_id'] == LOG_ID
    bad = client.post('/v1/log/start', json={'log_id': 'nope'})
    assert bad.status_code == 400 and bad.json()['error']['code'] == 'bad_request'
    extra = client.post('/v1/log/stop', json={'unknown': 1})
    assert extra.status_code == 400
    assert client.get('/v1/nothing').json()['error']['code'] == 'not_found'


def test_start_without_body_is_standalone(client: TestClient) -> None:
    """POST /v1/log/start with no body starts a standalone log."""
    response = client.post('/v1/log/start')
    assert response.status_code == 200
    assert response.json()['origin'] == 'standalone'


def test_file_download_with_range(backend: SampleBackend, client: TestClient) -> None:
    """Files support Range/206, If-Range, ETag and Last-Modified; recording logs are 409."""
    assert client.post('/v1/log/start', json={'log_id': LOG_ID}).status_code == 200
    busy = client.get(f'/v1/logs/{LOG_ID}/files/manifest.json')
    assert busy.status_code == 409 and busy.json()['error']['code'] == 'recording_in_progress'
    backend.session.close()

    payload = bytes(range(256)) * 4
    other = '2026-10-17T15-00-00Z_000000'
    record_log_with_id(backend, client, other, payload)
    url = f'/v1/logs/{other}/files/data.000.bin'
    full = client.get(url)
    assert full.status_code == 200
    assert full.content == payload
    assert full.headers['accept-ranges'] == 'bytes'
    assert full.headers['content-length'] == str(len(payload))
    assert 'etag' in full.headers and 'last-modified' in full.headers
    partial = client.get(url, headers={'Range': 'bytes=100-199'})
    assert partial.status_code == 206
    assert partial.content == payload[100:200]
    assert partial.headers['content-range'] == f'bytes 100-199/{len(payload)}'
    stale = client.get(url, headers={'Range': 'bytes=100-', 'If-Range': '"stale"'})
    assert stale.status_code == 200 and stale.content == payload
    fresh = client.get(url, headers={'Range': 'bytes=100-', 'If-Range': full.headers['etag']})
    assert fresh.status_code == 206 and fresh.content == payload[100:]


def test_path_traversal_is_404(backend: SampleBackend, client: TestClient) -> None:
    """Encoded traversal attempts never leave the log directory."""
    record_log(backend, client)
    for path in ('..%2F..%2Fconfig.yaml', '%2E%2E/%2E%2E/config.yaml', '..%2Fx', '%2Fetc%2Fpasswd'):
        response = client.get(f'/v1/logs/{LOG_ID}/files/{path}')
        assert response.status_code == 404, path
    assert client.get('/v1/logs/..%2F..%2F/files/config.yaml').status_code == 404


def test_listing_endpoints(backend: SampleBackend, client: TestClient) -> None:
    """GET /v1/logs and /v1/logs/{id}."""
    record_log(backend, client)
    logs = client.get('/v1/logs').json()['logs']
    assert [log['log_id'] for log in logs] == [LOG_ID]
    assert logs[0]['state'] == 'complete'
    assert 'manifest' not in logs[0]
    detail = client.get(f'/v1/logs/{LOG_ID}').json()
    assert detail['manifest']['segments'][0]['files'] == ['data.000.bin']
    assert client.get('/v1/logs/2026-01-01T00-00-00Z_ffffff').json()['error']['code'] == 'log_not_found'


def test_config_semantics(backend: SampleBackend, client: TestClient, tmp_path: Path) -> None:
    """ReadOnly → 422, writeOnly masked and null-means-unchanged, restart-required → health warn, 409 while recording."""
    document = client.get('/v1/config').json()
    assert document['values']['password'] is None
    assert document['schema']['properties']['bind_address']['readOnly'] is True
    assert document['schema']['properties']['port']['x-restart-required'] is True

    assert client.patch('/v1/config', json={'bind_address': 'x:2'}).json()['error']['code'] == 'validation_error'
    assert client.patch('/v1/config', json={'bind_address': '0.0.0.0:1'}).status_code == 200
    assert client.patch('/v1/config', json={'rate': 0}).status_code == 422
    assert client.patch('/v1/config', json={'nope': 1}).status_code == 422
    assert client.patch('/v1/config', json={'rate': 'x'}).status_code == 422

    assert client.patch('/v1/config', json={'rate': 20, 'password': None}).status_code == 200
    assert backend.config.values.rate == 20
    assert backend.config.values.password == 'hunter2'
    assert 'rate: 20' in (tmp_path / 'config.yaml').read_text()

    assert client.patch('/v1/config', json={'port': '/dev/ttyUSB0'}).status_code == 200
    health = client.get('/v1/status').json()['health']
    assert health['level'] == 'warn' and 'port' in health['message']

    client.post('/v1/log/start', json={'log_id': LOG_ID})
    busy = client.patch('/v1/config', json={'rate': 30})
    assert busy.status_code == 409 and busy.json()['error']['code'] == 'recording_in_progress'


def test_actions(backend: SampleBackend, client: TestClient) -> None:
    """Actions validate params (422), report unavailability (409) and unknown names (404)."""
    assert [a['name'] for a in client.get('/v1/actions').json()['actions']] == ['survey-in', 'nope']
    ok = client.post('/v1/actions/survey-in', json={'seconds': 5})
    assert ok.status_code == 200 and ok.json() == {'message': 'ok', 'data': {'seconds': 5}}
    assert client.post('/v1/actions/survey-in').status_code == 200
    invalid = client.post('/v1/actions/survey-in', json={'seconds': 0})
    assert invalid.status_code == 422 and invalid.json()['error']['details']['errors'][0]['path'] == 'seconds'
    unavailable = client.post('/v1/actions/nope', json={})
    assert unavailable.status_code == 409 and unavailable.json()['error']['message'] == 'not now'
    assert client.post('/v1/actions/missing', json={}).json()['error']['code'] == 'action_not_found'
    assert backend.ran == [('survey-in', {'seconds': 5}), ('survey-in', {})]


def test_write_fault_is_reported_as_health_error(backend: SampleBackend, client: TestClient) -> None:
    """Session faults override the backend's health."""
    client.post('/v1/log/start', json={'log_id': LOG_ID})
    backend.session.tick()
    backend.session.write_failed('disk full')
    health = client.get('/v1/status').json()['health']
    assert health == {'level': 'error', 'message': 'disk full'}
    assert client.get('/v1/status').json()['recording']['state'] == 'recording'


def test_api_error_envelope_helper() -> None:
    """ApiError.envelope matches the spec shape."""
    assert ApiError(409, 'x', 'y', {'a': 1}).envelope() == {'error': {'code': 'x', 'message': 'y', 'details': {'a': 1}}}
