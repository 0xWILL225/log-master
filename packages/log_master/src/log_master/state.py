"""The small active-recording state file (`/var/lib/log-master/state.json`).

This is the only state log-master persists besides its config: the active log and the
sources whose stop has not been confirmed yet. Everything else is fetched live.
"""

from pathlib import Path

from logserver.fsutil import atomic_write_bytes
from pydantic import BaseModel, Field


class ActiveLog(BaseModel):
    """The log currently recorded under log-master's control."""

    log_id: str
    participants: list[str]
    scheduled_start_unix_ns: int
    stop_at_unix_ns: int | None = None
    started_request_id: str | None = None


class PersistedState(BaseModel):
    """Content of the state file."""

    active_log: ActiveLog | None = None
    stop_pending: dict[str, str] = Field(default_factory=dict, description='Source name → log ID awaiting a stop.')


class StateFile:
    """Atomic load/save of :class:`PersistedState`."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def load(self) -> PersistedState:
        """Load the state; a missing file means no active log and no pending stops.

        Raises:
            ValueError: If the file exists but is not valid.
        """
        if not self.path.exists():
            return PersistedState()
        return PersistedState.model_validate_json(self.path.read_bytes())

    def save(self, state: PersistedState) -> None:
        """Atomically write the state, creating the parent directory if needed."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(self.path, state.model_dump_json(indent=2).encode() + b'\n')
