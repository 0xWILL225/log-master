"""The daemon's live objects, shared by all API routers."""

from dataclasses import dataclass

import httpx

from log_master.config import Config, ConfigFile
from log_master.poller import Poller
from log_master.recorder import Recorder
from log_master.state import StateFile


@dataclass
class Runtime:
    """Everything a request handler needs: config, HTTP client, poller and recorder."""

    config_file: ConfigFile
    http: httpx.AsyncClient
    poller: Poller
    recorder: Recorder

    @property
    def config(self) -> Config:
        """The current configuration."""
        return self.config_file.config

    @classmethod
    def create(cls, config_file: ConfigFile) -> 'Runtime':
        """Create the runtime; loads the state file named in the config.

        Raises:
            ValueError: If the state file is invalid.
        """
        http = httpx.AsyncClient(limits=httpx.Limits(max_connections=200, max_keepalive_connections=50))
        poller = Poller(config_file, http)
        recorder = Recorder(config_file, poller, http, StateFile(config_file.config.state_file))
        poller.after_poll = recorder.on_poll
        return cls(config_file=config_file, http=http, poller=poller, recorder=recorder)

    def config_changed(self) -> None:
        """Apply source-list changes to the poller."""
        self.poller.sync_sources()
