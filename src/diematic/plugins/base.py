"""Contract between the core and broker plugins."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from diematic.core.events import EventBus
from diematic.core.state import StateStore
from diematic.registers.loader import RegisterMap


class PluginError(Exception):
    """A plugin could not be found, configured or started."""


@dataclass(frozen=True)
class PluginContext:
    """Everything a plugin may use. Plugins never touch the bus: they only exchange events."""

    regmap: RegisterMap
    state: StateStore
    events: EventBus
    language: str
    config: dict[str, Any] = field(default_factory=dict)


class Plugin(ABC):
    """A bridge between the event bus and an external system (a broker, an API...).

    Constructors validate configuration and raise PluginError on bad settings. `run` serves
    until cancelled and is restarted by the supervisor if it fails, so it should simply raise
    when its connection is lost and let the supervisor retry.
    """

    def __init__(self, context: PluginContext) -> None:
        self.context = context

    @abstractmethod
    async def run(self) -> None: ...
