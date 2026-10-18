"""Config helper: a pydantic config model persisted to a YAML file and exposed as JSON Schema.

A server describes its configuration with a pydantic model. :class:`ConfigStore` loads and
persists it with `ruamel.yaml` round-trip I/O (comments in the user's `config.yaml` survive
edits), generates the JSON Schema served by `GET /v1/config`, and implements the
`PATCH /v1/config` semantics (partial update, validation, read-only and secret fields,
restart-required fields).

Property annotations are added with :func:`config_field`::

    class MyConfig(BaseModel):
        bind_address: str = config_field('0.0.0.0:8001', read_only=True)
        port: str = config_field('/dev/ttyACM0', restart_required=True)
        ntrip_password: str = config_field('', write_only=True)
"""

import copy
import io
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Generic, TypeVar

from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field, ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from logserver.errors import ApiError
from logserver.fsutil import atomic_write_bytes
from logserver.models import ConfigDocument

ModelT = TypeVar('ModelT', bound=BaseModel)

READ_ONLY = 'readOnly'
WRITE_ONLY = 'writeOnly'
RESTART_REQUIRED = 'x-restart-required'


def config_field(
    default: Any = ...,
    *,
    read_only: bool = False,
    write_only: bool = False,
    restart_required: bool = False,
    **kwargs: Any,
) -> Any:
    """Create a pydantic `Field` carrying the config annotations of the Logging Server API.

    Args:
        default: Default value (as for `pydantic.Field`).
        read_only: The value cannot be changed via the API (`readOnly`).
        write_only: The value is a secret: reported as null, only sent when edited (`writeOnly`).
        restart_required: Changes are persisted but only take effect after a restart
            (`x-restart-required`).
        **kwargs: Passed through to `pydantic.Field` (e.g. `title`, `description`, `ge`).
    """
    extra: dict[str, Any] = {}
    if read_only:
        extra[READ_ONLY] = True
    if write_only:
        extra[WRITE_ONLY] = True
    if restart_required:
        extra[RESTART_REQUIRED] = True
    return Field(default, json_schema_extra=extra or None, **kwargs)


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a pydantic JSON Schema with local `$ref`s replaced by their definitions.

    Simple form renderers then only need to handle nested `type: object` schemas. Recursive
    models are not supported.
    """
    defs = schema.get('$defs', {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if '$ref' in node:
                name = node['$ref'].removeprefix('#/$defs/')
                merged = {**resolve(defs[name]), **{k: resolve(v) for k, v in node.items() if k != '$ref'}}
                return merged
            return {k: resolve(v) for k, v in node.items() if k != '$defs'}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    return resolve(schema)


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Return `base` updated with `patch`, merging nested objects recursively."""
    merged = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def validate_json_schema(schema: dict[str, Any], instance: Any, what: str) -> None:
    """Validate `instance` against a JSON Schema (draft 2020-12).

    Raises:
        ApiError: 422 `validation_error` listing every violation as `{path, message}`.
    """
    errors = sorted(Draft202012Validator(schema).iter_errors(instance), key=lambda e: list(e.absolute_path))
    if errors:
        raise ApiError(
            422,
            'validation_error',
            f'{what} failed validation: {errors[0].message}',
            {'errors': [{'path': '.'.join(str(p) for p in e.absolute_path), 'message': e.message} for e in errors]},
        )


def yaml_round_trip() -> YAML:
    """Return a `ruamel.yaml` instance configured for comment-preserving round-trip I/O."""
    yaml = YAML(typ='rt')
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def load_yaml_document(path: Path) -> CommentedMap:
    """Load a YAML mapping with comments preserved; an empty file yields an empty mapping.

    Raises:
        ValueError: If the top-level YAML value is not a mapping.
    """
    document = yaml_round_trip().load(Path(path).read_text())
    if document is None:
        return CommentedMap()
    if not isinstance(document, CommentedMap):
        raise ValueError(f'{path}: top-level YAML value must be a mapping')
    return document


def write_yaml_document(path: Path, document: CommentedMap) -> None:
    """Atomically write a (round-trip) YAML document."""
    buffer = io.StringIO()
    yaml_round_trip().dump(document, buffer)
    atomic_write_bytes(Path(path), buffer.getvalue().encode())


def assign_preserving(target: CommentedMap, values: dict[str, Any]) -> None:
    """Write `values` into `target`, recursing into nested mappings so their comments survive."""
    for key, value in values.items():
        if isinstance(value, dict) and isinstance(target.get(key), CommentedMap):
            assign_preserving(target[key], value)
            for stale in [k for k in target[key] if k not in value]:
                del target[key][stale]
        else:
            target[key] = value


def to_plain(node: Any) -> Any:
    """Convert ruamel containers into plain dicts/lists."""
    if isinstance(node, dict):
        return {str(k): to_plain(v) for k, v in node.items()}
    if isinstance(node, list):
        return [to_plain(v) for v in node]
    return node


def _annotated(schema: dict[str, Any], annotation: str) -> set[str]:
    """Return the top-level property names carrying `annotation: true`."""
    return {name for name, prop in schema.get('properties', {}).items() if prop.get(annotation)}


def clean_patch(schema: dict[str, Any], patch: dict[str, Any], current: dict[str, Any], prefix: str) -> dict[str, Any]:
    """Reject unknown and read-only changes and drop null secrets (meaning "unchanged") from a patch."""
    properties = schema.get('properties', {})
    cleaned: dict[str, Any] = {}
    for key, value in patch.items():
        path = f'{prefix}{key}'
        prop = properties.get(key)
        if prop is None:
            raise ApiError(
                422,
                'validation_error',
                f'unknown config field {path!r}',
                {'errors': [{'path': path, 'message': 'unknown field'}]},
            )
        if prop.get(WRITE_ONLY) and value is None:
            continue
        if prop.get(READ_ONLY):
            if value != current.get(key):
                raise ApiError(
                    422,
                    'validation_error',
                    f'config field {path!r} is read-only',
                    {'errors': [{'path': path, 'message': 'read-only'}]},
                )
            continue
        if isinstance(value, dict) and prop.get('type') == 'object' and isinstance(current.get(key), dict):
            cleaned[key] = clean_patch(prop, value, current[key], f'{path}.')
        else:
            cleaned[key] = value
    return cleaned


def mask_write_only(schema: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Return `values` with every `writeOnly` property (at any depth) replaced by null."""
    masked: dict[str, Any] = {}
    properties = schema.get('properties', {})
    for key, value in values.items():
        prop = properties.get(key, {})
        if prop.get(WRITE_ONLY):
            masked[key] = None
        elif isinstance(value, dict) and prop.get('type') == 'object':
            masked[key] = mask_write_only(prop, value)
        else:
            masked[key] = value
    return masked


class ConfigStore(Generic[ModelT]):
    """A server's configuration: pydantic model + JSON Schema + comment-preserving `config.yaml`.

    Args:
        model_type: Pydantic model describing the configuration. Use :func:`config_field` for
            annotated properties.
        path: The server's own `config.yaml`. Created from `defaults` if it does not exist.
        defaults: Values used when creating a missing file.
        on_change: Called as `on_change(old, new, changed)` after a successful patch with the
            names of the changed top-level fields that take effect live (restart-required fields
            are excluded). It may raise :class:`ApiError` to reject the change, in which case
            nothing is persisted.
    """

    def __init__(
        self,
        model_type: type[ModelT],
        path: Path | str,
        *,
        defaults: dict[str, Any] | None = None,
        on_change: Callable[[ModelT, ModelT, set[str]], None] | None = None,
    ):
        self._model_type = model_type
        self._path = Path(path)
        self._lock = threading.Lock()
        self.on_change = on_change
        self._schema = inline_refs(model_type.model_json_schema())
        if self._path.exists():
            self._document = load_yaml_document(self._path)
            self._values = model_type.model_validate(to_plain(self._document))
        else:
            self._values = model_type.model_validate(defaults or {})
            self._document = CommentedMap(self._values.model_dump(mode='json'))
            self._path.parent.mkdir(parents=True, exist_ok=True)
            write_yaml_document(self._path, self._document)
        self._startup_values = self._values.model_dump(mode='json')

    @property
    def path(self) -> Path:
        """Path of the persisted `config.yaml`."""
        return self._path

    @property
    def values(self) -> ModelT:
        """Current (persisted) configuration."""
        with self._lock:
            return self._values

    @property
    def schema(self) -> dict[str, Any]:
        """JSON Schema (draft 2020-12) of the configuration, with `$ref`s inlined."""
        return copy.deepcopy(self._schema)

    @property
    def restart_pending(self) -> list[str]:
        """Restart-required fields whose persisted value differs from the value at startup."""
        with self._lock:
            current = self._values.model_dump(mode='json')
        return sorted(
            k for k in _annotated(self._schema, RESTART_REQUIRED) if current.get(k) != self._startup_values.get(k)
        )

    def document(self) -> ConfigDocument:
        """Return the `{schema, values}` document of `GET /v1/config` (secrets masked)."""
        with self._lock:
            values = self._values.model_dump(mode='json')
        return ConfigDocument(json_schema=self.schema, values=mask_write_only(self._schema, values))

    def patch(self, partial: dict[str, Any]) -> ConfigDocument:
        """Apply a partial update (`PATCH /v1/config`): validate, apply live, persist.

        Raises:
            ApiError: 422 `validation_error` for unknown or read-only fields and schema violations.
        """
        with self._lock:
            old = self._values
            old_values = old.model_dump(mode='json')
            merged = deep_merge(old_values, clean_patch(self._schema, partial, old_values, ''))
            validate_json_schema(self._schema, merged, 'config')
            new = self._validate_model(merged)
            new_values = new.model_dump(mode='json')
            changed = {k for k in new_values if new_values[k] != old_values.get(k)}
            if changed:
                live = changed - _annotated(self._schema, RESTART_REQUIRED)
                if self.on_change is not None and live:
                    self.on_change(old, new, live)
                assign_preserving(self._document, {k: new_values[k] for k in changed})
                write_yaml_document(self._path, self._document)
                self._values = new
        return self.document()

    def force_update(self, values: dict[str, Any]) -> None:
        """Set values without the read-only checks or `on_change` (for the server's own startup overrides)."""
        with self._lock:
            new = self._validate_model(deep_merge(self._values.model_dump(mode='json'), values))
            new_values = new.model_dump(mode='json')
            assign_preserving(self._document, {k: new_values[k] for k in values})
            write_yaml_document(self._path, self._document)
            self._values = new
            self._startup_values = new_values

    def _validate_model(self, values: dict[str, Any]) -> ModelT:
        try:
            return self._model_type.model_validate(values)
        except ValidationError as exc:
            errors = [{'path': '.'.join(str(p) for p in e['loc']), 'message': e['msg']} for e in exc.errors()]
            raise ApiError(
                422, 'validation_error', f'config failed validation: {errors[0]["message"]}', {'errors': errors}
            )
