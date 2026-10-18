"""Parsing `key=value` arguments according to a JSON Schema (config, settings and action params)."""

import json
from typing import Any

_TRUE = {'true', 'yes', 'on', '1'}
_FALSE = {'false', 'no', 'off', '0'}
_TYPE_HINTS = {'integer': 'INT', 'number': 'NUM', 'string': 'TEXT'}


def _variants(prop: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Unwrap `anyOf: [X, {type: null}]`; return (X, nullable)."""
    options = prop.get('anyOf')
    if not options:
        types = prop.get('type')
        if isinstance(types, list):
            non_null = [t for t in types if t != 'null']
            return {**prop, 'type': non_null[0] if len(non_null) == 1 else non_null}, 'null' in types
        return prop, False
    non_null = [option for option in options if option.get('type') != 'null']
    nullable = len(non_null) != len(options)
    if len(non_null) == 1:
        return {**{k: v for k, v in prop.items() if k != 'anyOf'}, **non_null[0]}, nullable
    return prop, nullable


def parse_value(prop: dict[str, Any], text: str) -> Any:
    """Convert command-line text into a value of the type described by `prop`.

    Raises:
        ValueError: If `text` does not fit the schema type.
    """
    schema, nullable = _variants(prop)
    if nullable and text.lower() in ('null', 'none'):
        return None
    kind = schema.get('type')
    if kind == 'integer':
        value: Any = int(text)
    elif kind == 'number':
        value = float(text)
    elif kind == 'boolean':
        lowered = text.lower()
        if lowered not in _TRUE | _FALSE:
            raise ValueError(f'expected a boolean, got {text!r}')
        value = lowered in _TRUE
    elif kind == 'string':
        value = text
    elif kind in ('array', 'object'):
        value = json.loads(text)
    else:
        try:
            value = json.loads(text)
        except ValueError:
            value = text
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(f'expected one of {", ".join(map(str, schema["enum"]))}, got {text!r}')
    return value


def property_at(schema: dict[str, Any], dotted: str) -> dict[str, Any]:
    """Return the sub-schema of a dotted property path such as `poll.interval_s`.

    Raises:
        KeyError: If the path does not exist in the schema.
    """
    node = schema
    for part in dotted.split('.'):
        node = _variants(node)[0].get('properties', {})[part]
    return node


def build_patch(schema: dict[str, Any], assignments: list[str]) -> dict[str, Any]:
    """Turn `key=value` assignments (dotted keys for nested objects) into a nested partial object.

    Raises:
        ValueError: For malformed assignments, unknown keys or values of the wrong type.
    """
    patch: dict[str, Any] = {}
    for assignment in assignments:
        key, sep, text = assignment.partition('=')
        if not sep or not key:
            raise ValueError(f'expected key=value, got {assignment!r}')
        try:
            prop = property_at(schema, key)
        except KeyError:
            raise ValueError(f'unknown key {key!r}')
        try:
            value = parse_value(prop, text)
        except ValueError as exc:
            raise ValueError(f'{key}: {exc}')
        node = patch
        *parents, leaf = key.split('.')
        for parent in parents:
            node = node.setdefault(parent, {})
        node[leaf] = value
    return patch


def flatten(values: dict[str, Any], prefix: str = '') -> dict[str, Any]:
    """Flatten nested objects into dotted keys."""
    flat: dict[str, Any] = {}
    for key, value in values.items():
        if isinstance(value, dict):
            flat.update(flatten(value, f'{prefix}{key}.'))
        else:
            flat[f'{prefix}{key}'] = value
    return flat


def flat_properties(schema: dict[str, Any], prefix: str = '') -> dict[str, dict[str, Any]]:
    """Map dotted keys to leaf property schemas."""
    flat: dict[str, dict[str, Any]] = {}
    for key, prop in _variants(schema)[0].get('properties', {}).items():
        if _variants(prop)[0].get('type') == 'object' and 'properties' in _variants(prop)[0]:
            flat.update(flat_properties(prop, f'{prefix}{key}.'))
        else:
            flat[f'{prefix}{key}'] = prop
    return flat


def parse_action_args(schema: dict[str, Any], args: list[str]) -> dict[str, Any]:
    """Parse `--param value` / `--flag` / `--no-flag` / `--param=value` arguments for an action.

    Parameter names use dashes on the command line (`--accuracy-mm` → `accuracy_mm`).

    Raises:
        ValueError: For unknown parameters, missing values or values of the wrong type.
    """
    properties = schema.get('properties', {})
    params: dict[str, Any] = {}
    index = 0
    while index < len(args):
        token = args[index]
        if not token.startswith('--'):
            raise ValueError(f'unexpected argument {token!r}')
        key, sep, text = token[2:].partition('=')
        name = key.replace('-', '_')
        negated = False
        if name not in properties and name.startswith('no_') and name[3:] in properties:
            name, negated = name[3:], True
        if name not in properties:
            available = ', '.join('--' + p.replace('_', '-') for p in properties) or 'none'
            raise ValueError(f'unknown parameter --{key} (available: {available})')
        prop = properties[name]
        is_bool = _variants(prop)[0].get('type') == 'boolean'
        if negated:
            if not is_bool:
                raise ValueError(f'--no-{key[3:]} is only valid for boolean parameters')
            params[name] = False
        elif is_bool and not sep:
            params[name] = True
        else:
            if not sep:
                index += 1
                if index >= len(args):
                    raise ValueError(f'--{key} needs a value')
                text = args[index]
            try:
                params[name] = parse_value(prop, text)
            except ValueError as exc:
                raise ValueError(f'--{key}: {exc}')
        index += 1
    return params


def describe_param(name: str, prop: dict[str, Any]) -> str:
    """Summarize an action parameter, e.g. `--seconds INT (min 1, default 60)`."""
    schema, _ = _variants(prop)
    kind = schema.get('type')
    flag = '--' + name.replace('_', '-')
    if kind == 'boolean':
        text = f'{flag}/--no-{name.replace("_", "-")}'
    elif 'enum' in schema:
        text = f'{flag} {{{"|".join(map(str, schema["enum"]))}}}'
    else:
        text = f'{flag} {_TYPE_HINTS.get(kind, "JSON")}'
    notes = []
    if 'minimum' in schema:
        notes.append(f'min {schema["minimum"]}')
    if 'maximum' in schema:
        notes.append(f'max {schema["maximum"]}')
    if 'default' in schema:
        notes.append(f'default {schema["default"]}')
    return f'{text} ({", ".join(notes)})' if notes else text
