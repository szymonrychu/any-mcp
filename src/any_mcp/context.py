"""What the core hands a plugin: its configuration, HTTP client, logger and reload trigger."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict


class NoConfig(BaseModel):
    """The Config of a plugin that takes no configuration."""

    model_config = ConfigDict(extra="forbid")


@dataclass
class PluginContext:
    """Per-plugin runtime context. ``config`` is an instance of the plugin's ``Config`` model.

    ``config_dir`` is the directory of the config file (None when there is none).
    """

    name: str
    namespace: str
    config: BaseModel
    http: httpx.AsyncClient
    log: logging.Logger
    response_budget_chars: int
    config_dir: Path | None = None
    _reload: Callable[[], None] | None = field(default=None, repr=False, compare=False)

    def _bind_reload(self, callback: Callable[[], None]) -> None:
        """Called by the registry; ``request_reload`` then invokes ``callback``."""
        self._reload = callback

    def request_reload(self) -> None:
        """Ask the core to re-run ``load_tools``. A no-op until the registry binds a callback."""
        if self._reload is None:
            self.log.debug(
                "request_reload ignored: plugin %s is not bound to a registry", self.name
            )
            return
        self._reload()
