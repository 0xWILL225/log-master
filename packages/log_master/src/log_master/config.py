"""log-master configuration: pydantic models and comment-preserving `config.yaml` I/O.

The file is edited by the UI and CLI through :class:`ConfigFile`. Every edit is validated,
written atomically with `ruamel.yaml` round-trip I/O (comments survive) and applied live,
except `http.bind` and `state_file`, which take effect after a restart.
"""

import threading
from pathlib import Path
from typing import Any

from logserver.config import (
    assign_preserving,
    clean_patch,
    config_field,
    deep_merge,
    inline_refs,
    load_yaml_document,
    mask_write_only,
    to_plain,
    validate_json_schema,
    write_yaml_document,
)
from logserver.errors import ApiError
from logserver.models import ConfigDocument
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from ruamel.yaml.comments import CommentedMap, CommentedSeq

SOURCE_NAME_PATTERN = r'^[a-z0-9][a-z0-9_-]{0,31}$'
DEFAULT_CONFIG_PATH = Path('/etc/log-master/config.yaml')


class _Section(BaseModel):
    model_config = ConfigDict(extra='forbid')


class HttpSettings(_Section):
    """HTTP server settings."""

    bind: str = config_field(
        '0.0.0.0:8080', restart_required=True, pattern=r'^.+:\d{1,5}$', title='Bind address', description='host:port'
    )


class PollSettings(_Section):
    """Source polling."""

    interval_s: float = config_field(1.0, gt=0, title='Poll interval (s)')
    timeout_s: float = config_field(0.8, gt=0, title='Poll timeout (s)')
    offline_after_failures: int = config_field(2, ge=1, title='Offline after failures')


class ListingSettings(_Section):
    """Log listing and proxied requests."""

    timeout_s: float = config_field(5.0, gt=0, title='Listing timeout (s)')


class RecordingSettings(_Section):
    """Recording coordination."""

    start_lead_s: float = config_field(
        2.0, ge=0, title='Start lead (s)', description='Delay from request to synchronized start.'
    )
    stop_lead_s: float = config_field(
        1.0, ge=0, title='Stop lead (s)', description='Delay from request to synchronized stop.'
    )
    late_start_warn_ms: int = config_field(500, ge=0, title='Late start warning (ms)')


class DiskSettings(_Section):
    """Low-disk-space warnings."""

    warn_free_bytes: int = config_field(5 * 1024**3, ge=0, title='Warn below free bytes')
    warn_free_fraction: float = config_field(0.10, ge=0, le=1, title='Warn below free fraction')


class Settings(_Section):
    """All non-source settings (`GET/PATCH /api/v1/settings`)."""

    model_config = ConfigDict(extra='forbid', title='log-master settings')

    http: HttpSettings = Field(default_factory=HttpSettings, title='HTTP')
    poll: PollSettings = Field(default_factory=PollSettings, title='Polling')
    listing: ListingSettings = Field(default_factory=ListingSettings, title='Listing')
    recording: RecordingSettings = Field(default_factory=RecordingSettings, title='Recording')
    disk: DiskSettings = Field(default_factory=DiskSettings, title='Disk')
    state_file: str = config_field('/var/lib/log-master/state.json', restart_required=True, title='State file')


class SourceConfig(_Section):
    """A configured logging server."""

    name: str = Field(pattern=SOURCE_NAME_PATTERN, description='Unique; used in zip paths.')
    url: str = Field(pattern=r'^https?://[^/\s]+(/\S*)?$', description='Base URL of the logging server.')
    enabled_by_default: bool = True

    @field_validator('url')
    @classmethod
    def _strip_slash(cls, url: str) -> str:
        return url.rstrip('/')


class SourceUpdate(_Section):
    """Partial update of a source (`PUT /api/v1/sources/{name}`)."""

    name: str | None = Field(None, pattern=SOURCE_NAME_PATTERN)
    url: str | None = Field(None, pattern=r'^https?://[^/\s]+(/\S*)?$')
    enabled_by_default: bool | None = None


class Config(Settings):
    """The complete `config.yaml`."""

    sources: list[SourceConfig] = Field(default_factory=list)

    @model_validator(mode='after')
    def _unique_names(self) -> 'Config':
        names = [source.name for source in self.sources]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f'duplicate source names: {", ".join(duplicates)}')
        return self

    def source(self, name: str) -> SourceConfig | None:
        """Return the source called `name`, if configured."""
        return next((source for source in self.sources if source.name == name), None)


SETTINGS_SCHEMA: dict[str, Any] = inline_refs(Settings.model_json_schema())
_SETTINGS_KEYS = set(Settings.model_fields)
_RESTART_REQUIRED_PATHS = (('http', 'bind'), ('state_file',))


def parse_bind(bind: str) -> tuple[str, int]:
    """Split `host:port` (IPv6 hosts may be bracketed) into host and port."""
    host, _, port = bind.rpartition(':')
    return host.strip('[]'), int(port)


def _validation_error(exc: ValidationError) -> ApiError:
    errors = [{'path': '.'.join(str(p) for p in e['loc']), 'message': e['msg']} for e in exc.errors()]
    return ApiError(422, 'validation_error', f'invalid configuration: {errors[0]["message"]}', {'errors': errors})


def _get_path(values: dict[str, Any], path: tuple[str, ...]) -> Any:
    for key in path:
        values = values[key]
    return values


class ConfigFile:
    """The log-master `config.yaml`: validated model + comment-preserving persistence.

    Raises:
        FileNotFoundError: If `path` does not exist.
        pydantic.ValidationError: If the file content is invalid.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f'config file {self.path} does not exist (see config.example.yaml)')
        self._lock = threading.Lock()
        self._document = load_yaml_document(self.path)
        self._config = Config.model_validate(to_plain(self._document))
        self._startup = self._config.model_dump(mode='json')

    @property
    def config(self) -> Config:
        """The current configuration."""
        with self._lock:
            return self._config

    @property
    def restart_pending(self) -> list[str]:
        """Restart-required settings whose value changed since startup, as dotted paths."""
        current = self.config.model_dump(mode='json')
        return ['.'.join(p) for p in _RESTART_REQUIRED_PATHS if _get_path(current, p) != _get_path(self._startup, p)]

    def settings_document(self) -> ConfigDocument:
        """Return `{schema, values}` of the non-source settings."""
        values = self.config.model_dump(mode='json', exclude={'sources'})
        return ConfigDocument(json_schema=SETTINGS_SCHEMA, values=mask_write_only(SETTINGS_SCHEMA, values))

    def patch_settings(self, partial: dict[str, Any]) -> ConfigDocument:
        """Apply a partial update to the non-source settings and persist it.

        Raises:
            ApiError: 422 `validation_error`.
        """
        with self._lock:
            old = self._config.model_dump(mode='json')
            current_settings = {k: v for k, v in old.items() if k in _SETTINGS_KEYS}
            merged = deep_merge(current_settings, clean_patch(SETTINGS_SCHEMA, partial, current_settings, ''))
            validate_json_schema(SETTINGS_SCHEMA, merged, 'settings')
            new = self._validated({**merged, 'sources': old['sources']})
            new_values = new.model_dump(mode='json')
            changed = {k: new_values[k] for k in _SETTINGS_KEYS if new_values[k] != old[k]}
            if changed:
                assign_preserving(self._document, changed)
                self._commit(new)
        return self.settings_document()

    def add_source(self, source: SourceConfig) -> SourceConfig:
        """Append a source.

        Raises:
            ApiError: 409 `source_exists`.
        """
        with self._lock:
            if self._config.source(source.name) is not None:
                raise ApiError(409, 'source_exists', f'source {source.name!r} already exists')
            new = self._validated(
                {**self._config.model_dump(mode='json'), 'sources': [*self._sources(), source.model_dump(mode='json')]}
            )
            self._sources_seq().append(CommentedMap(source.model_dump(mode='json')))
            self._commit(new)
            return source

    def update_source(self, name: str, update: SourceUpdate) -> SourceConfig:
        """Change a source's name, URL and/or `enabled_by_default`.

        Raises:
            ApiError: 404 `source_not_found`, 409 `source_exists`, 422 `validation_error`.
        """
        with self._lock:
            index = self._index(name)
            changes = update.model_dump(exclude_none=True)
            if changes.get('name', name) != name and self._config.source(changes['name']) is not None:
                raise ApiError(409, 'source_exists', f'source {changes["name"]!r} already exists')
            sources = self._sources()
            sources[index] = {**sources[index], **changes}
            new = self._validated({**self._config.model_dump(mode='json'), 'sources': sources})
            assign_preserving(self._sources_seq()[index], new.sources[index].model_dump(mode='json'))
            self._commit(new)
            return new.sources[index]

    def remove_source(self, name: str) -> None:
        """Remove a source.

        Raises:
            ApiError: 404 `source_not_found`.
        """
        with self._lock:
            index = self._index(name)
            sources = self._sources()
            del sources[index]
            new = self._validated({**self._config.model_dump(mode='json'), 'sources': sources})
            del self._sources_seq()[index]
            self._commit(new)

    def _sources(self) -> list[dict[str, Any]]:
        return [source.model_dump(mode='json') for source in self._config.sources]

    def _sources_seq(self) -> CommentedSeq:
        if not isinstance(self._document.get('sources'), CommentedSeq):
            self._document['sources'] = CommentedSeq()
        return self._document['sources']

    def _index(self, name: str) -> int:
        for index, source in enumerate(self._config.sources):
            if source.name == name:
                return index
        raise ApiError(404, 'source_not_found', f'no source {name!r}')

    @staticmethod
    def _validated(values: dict[str, Any]) -> Config:
        try:
            return Config.model_validate(values)
        except ValidationError as exc:
            raise _validation_error(exc)

    def _commit(self, new: Config) -> None:
        write_yaml_document(self.path, self._document)
        self._config = new
