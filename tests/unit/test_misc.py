"""Unit tests of log IDs, log merging, CLI value parsing and the fake's rate parser."""

import argparse

import pytest
from log_master.cli.values import build_patch, describe_param, flatten, parse_action_args, parse_value
from log_master.listing import merge_parts
from log_master.models import MergedLogPart
from logserver.fake.main import parse_rate
from logserver.ids import generate_log_id, is_valid_log_id, log_id_timestamp_ns
from logserver.models import Segment

ACTION_SCHEMA = {
    'type': 'object',
    'properties': {
        'seconds': {'type': 'integer', 'minimum': 1},
        'accuracy_mm': {'type': 'integer'},
        'immediate': {'type': 'boolean', 'default': False},
        'mode': {'type': 'string', 'enum': ['a', 'b']},
        'ratio': {'anyOf': [{'type': 'number'}, {'type': 'null'}]},
    },
}


def test_log_ids() -> None:
    """Generated IDs are valid, encode the UTC second and validate strictly."""
    log_id = generate_log_id(1_792_245_835_123_456_789)
    assert log_id.startswith('2026-10-17T14-03-55Z_')
    assert is_valid_log_id(log_id)
    assert log_id_timestamp_ns(log_id) == 1_792_245_835_000_000_000
    for bad in (
        '2026-10-17T14-03-55Z_A1B2C3',
        '2026-10-17T14-03-55Z_a1b2c',
        '2026-10-17 14-03-55Z_a1b2c3',
        '../x',
        '٢٠٢٦-10-17T14-03-55Z_a1b2c3',
    ):
        assert not is_valid_log_id(bad)
    assert len({generate_log_id(0) for _ in range(50)}) > 1


def part(
    source: str, state: str, start: int | None, end: int | None, segments: int = 1, origin: str = 'log-master'
) -> MergedLogPart:
    """Build a log part for merging."""
    return MergedLogPart(
        source=source,
        origin=origin,
        state=state,
        start_unix_ns=start,
        end_unix_ns=end,
        segments=[Segment(index=i) for i in range(segments)],
        total_bytes=100,
    )


def test_merge_parts() -> None:
    """Start/end/duration/size and flags are aggregated over parts."""
    merged = merge_parts(
        'id', [part('rover', 'complete', 10, 100), part('base', 'interrupted', 5, 90, segments=2)], None
    )
    assert (merged.start_unix_ns, merged.end_unix_ns, merged.duration_ns, merged.total_bytes) == (5, 100, 95, 200)
    assert merged.incomplete and merged.gaps and not merged.recording and not merged.standalone
    assert [p.source for p in merged.parts] == ['base', 'rover']

    active = merge_parts('id', [part('rover', 'recording', 10, None, origin='standalone')], ['rover', 'base'])
    assert active.recording and active.standalone and active.incomplete
    assert active.missing_sources == ['base']
    assert active.duration_ns is None


def test_parse_value() -> None:
    """Values are converted per schema type."""
    assert parse_value({'type': 'integer'}, '5') == 5
    assert parse_value({'type': 'number'}, '0.5') == 0.5
    assert parse_value({'type': 'boolean'}, 'yes') is True
    assert parse_value({'anyOf': [{'type': 'string'}, {'type': 'null'}]}, 'null') is None
    assert parse_value({'type': 'array'}, '[1, 2]') == [1, 2]
    with pytest.raises(ValueError):
        parse_value({'type': 'integer'}, 'x')
    with pytest.raises(ValueError):
        parse_value({'type': 'string', 'enum': ['a']}, 'b')


def test_build_patch_with_dotted_keys() -> None:
    """key=value assignments become a nested partial object."""
    schema = {
        'type': 'object',
        'properties': {
            'poll': {'type': 'object', 'properties': {'interval_s': {'type': 'number'}}},
            'name': {'type': 'string'},
        },
    }
    assert build_patch(schema, ['poll.interval_s=0.5', 'name=a=b']) == {'poll': {'interval_s': 0.5}, 'name': 'a=b'}
    for bad in (['nope=1'], ['poll.interval_s=x'], ['novalue']):
        with pytest.raises(ValueError):
            build_patch(schema, bad)
    assert flatten({'a': {'b': 1}, 'c': 2}) == {'a.b': 1, 'c': 2}


def test_parse_action_args() -> None:
    """--param value, --flag, --no-flag and --param=value forms are supported."""
    args = ['--seconds', '300', '--accuracy-mm=20', '--immediate', '--mode', 'b', '--ratio', 'null']
    assert parse_action_args(ACTION_SCHEMA, args) == {
        'seconds': 300,
        'accuracy_mm': 20,
        'immediate': True,
        'mode': 'b',
        'ratio': None,
    }
    assert parse_action_args(ACTION_SCHEMA, ['--no-immediate']) == {'immediate': False}
    for bad in (['--unknown', '1'], ['--seconds'], ['positional'], ['--seconds', 'x'], ['--no-seconds']):
        with pytest.raises(ValueError):
            parse_action_args(ACTION_SCHEMA, bad)
    assert describe_param('accuracy_mm', ACTION_SCHEMA['properties']['accuracy_mm']) == '--accuracy-mm INT'
    assert describe_param('seconds', ACTION_SCHEMA['properties']['seconds']) == '--seconds INT (min 1)'


def test_parse_rate() -> None:
    """Data rates accept SI and binary units."""
    assert parse_rate('200kB/s') == 200_000
    assert parse_rate('1MiB') == 1024 * 1024
    assert parse_rate('5000') == 5000
    with pytest.raises(argparse.ArgumentTypeError):
        parse_rate('fast')
