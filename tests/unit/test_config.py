"""Unit tests of config handling: comment-preserving round trips, schemas and validation."""

from pathlib import Path

import pytest
from log_master.config import SETTINGS_SCHEMA, ConfigFile, SourceConfig, SourceUpdate, parse_bind
from logserver.config import ConfigStore, config_field, inline_refs
from logserver.errors import ApiError
from pydantic import BaseModel, ValidationError

LOG_MASTER_YAML = """\
# top comment
http:
  bind: "0.0.0.0:8080"   # bind comment

poll:
  interval_s: 1.0        # interval comment
  timeout_s: 0.8

state_file: /tmp/state.json

sources:
  - name: rover          # rover comment
    url: http://robot-pc.local:8001
    enabled_by_default: true
  # base is on the pi
  - name: base
    url: http://base-pi.local:8002
"""


@pytest.fixture
def config_file(tmp_path: Path) -> ConfigFile:
    """A log-master config with comments."""
    path = tmp_path / 'config.yaml'
    path.write_text(LOG_MASTER_YAML)
    return ConfigFile(path)


def test_defaults_and_bind_parsing(config_file: ConfigFile) -> None:
    """Missing sections get defaults; bind addresses split into host and port."""
    config = config_file.config
    assert config.recording.start_lead_s == 2.0
    assert config.disk.warn_free_bytes == 5 * 1024**3
    assert config.sources[1].enabled_by_default is True
    assert parse_bind('0.0.0.0:8080') == ('0.0.0.0', 8080)
    assert parse_bind('[::]:9000') == ('::', 9000)


def test_settings_patch_preserves_comments(config_file: ConfigFile) -> None:
    """PATCH settings updates values in place and keeps every comment."""
    document = config_file.patch_settings({'poll': {'interval_s': 0.5}, 'recording': {'late_start_warn_ms': 250}})
    assert document.values['poll']['interval_s'] == 0.5
    text = config_file.path.read_text()
    for comment in ('# top comment', '# bind comment', '# interval comment', '# rover comment', '# base is on the pi'):
        assert comment in text
    assert 'interval_s: 0.5' in text
    assert 'late_start_warn_ms: 250' in text
    assert ConfigFile(config_file.path).config.poll.interval_s == 0.5


def test_settings_validation(config_file: ConfigFile) -> None:
    """Invalid settings are rejected with 422 and nothing is written."""
    before = config_file.path.read_text()
    for patch in (
        {'poll': {'interval_s': 0}},
        {'poll': {'nope': 1}},
        {'disk': {'warn_free_fraction': 2}},
        {'sources': []},
    ):
        with pytest.raises(ApiError) as excinfo:
            config_file.patch_settings(patch)
        assert excinfo.value.status_code == 422, patch
    assert config_file.path.read_text() == before


def test_restart_required_settings(config_file: ConfigFile) -> None:
    """http.bind is marked restart-required and reported until the restart."""
    assert SETTINGS_SCHEMA['properties']['http']['properties']['bind']['x-restart-required'] is True
    assert config_file.restart_pending == []
    config_file.patch_settings({'http': {'bind': '0.0.0.0:9090'}})
    assert config_file.restart_pending == ['http.bind']
    config_file.patch_settings({'http': {'bind': '0.0.0.0:8080'}})
    assert config_file.restart_pending == []


def test_source_editing_preserves_comments(config_file: ConfigFile) -> None:
    """Adding, editing and removing sources keeps the other comments."""
    config_file.add_source(SourceConfig(name='imu', url='http://robot-pc.local:8003/', enabled_by_default=False))
    assert config_file.config.source('imu').url == 'http://robot-pc.local:8003'
    config_file.update_source('rover', SourceUpdate(url='http://10.0.0.2:8001'))
    config_file.remove_source('base')
    text = config_file.path.read_text()
    assert '# rover comment' in text
    assert 'http://10.0.0.2:8001' in text
    assert 'base-pi' not in text
    reloaded = ConfigFile(config_file.path).config
    assert [s.name for s in reloaded.sources] == ['rover', 'imu']
    assert reloaded.sources[1].enabled_by_default is False


def test_source_editing_errors(config_file: ConfigFile) -> None:
    """Duplicate names → 409, unknown → 404, invalid names/URLs → validation errors."""
    with pytest.raises(ApiError) as excinfo:
        config_file.add_source(SourceConfig(name='rover', url='http://x:1'))
    assert excinfo.value.status_code == 409
    with pytest.raises(ApiError) as excinfo:
        config_file.update_source('rover', SourceUpdate(name='base'))
    assert excinfo.value.status_code == 409
    with pytest.raises(ApiError) as excinfo:
        config_file.remove_source('nope')
    assert excinfo.value.status_code == 404
    for bad in (
        {'name': 'Bad Name', 'url': 'http://x:1'},
        {'name': 'ok', 'url': 'ftp://x'},
        {'name': '-x', 'url': 'http://x'},
    ):
        with pytest.raises(ValidationError):
            SourceConfig.model_validate(bad)


def test_duplicate_sources_in_file_are_rejected(tmp_path: Path) -> None:
    """A config with duplicate source names fails to load."""
    path = tmp_path / 'config.yaml'
    path.write_text('sources:\n  - {name: a, url: "http://x:1"}\n  - {name: a, url: "http://y:1"}\n')
    with pytest.raises(ValidationError):
        ConfigFile(path)


def test_missing_config_file_is_an_error(tmp_path: Path) -> None:
    """log-master refuses to start without a config file."""
    with pytest.raises(FileNotFoundError):
        ConfigFile(tmp_path / 'missing.yaml')


class Nested(BaseModel):
    """Nested sample section."""

    level: int = 1


class StoreModel(BaseModel):
    """Sample server config."""

    name: str = config_field('x', read_only=True)
    nested: Nested = Nested()
    secret: str = config_field('s3cret', write_only=True)


def test_config_store_round_trip(tmp_path: Path) -> None:
    """ConfigStore creates the file, inlines refs and keeps comments on patch."""
    path = tmp_path / 'server.yaml'
    store = ConfigStore(StoreModel, path)
    assert path.exists()
    path.write_text('# keep me\nname: x\nnested:\n  level: 1  # level comment\nsecret: s3cret\n')
    store = ConfigStore(StoreModel, path)
    assert '$defs' not in store.schema
    assert store.schema['properties']['nested']['properties']['level']['type'] == 'integer'
    store.patch({'nested': {'level': 3}})
    text = path.read_text()
    assert '# keep me' in text and '# level comment' in text and 'level: 3' in text
    assert store.document().values == {'name': 'x', 'nested': {'level': 3}, 'secret': None}


def test_inline_refs_keeps_sibling_keywords() -> None:
    """Sibling keywords of a $ref (e.g. title) survive inlining."""
    schema = {
        '$defs': {'A': {'type': 'object', 'title': 'A'}},
        'properties': {'a': {'$ref': '#/$defs/A', 'title': 'Alpha'}},
    }
    assert inline_refs(schema) == {'properties': {'a': {'type': 'object', 'title': 'Alpha'}}}
