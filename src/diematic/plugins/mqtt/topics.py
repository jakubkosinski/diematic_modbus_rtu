"""Topic layout of the MQTT plugin and parsing of incoming command topics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ONLINE = "online"
OFFLINE = "offline"

CommandKind = Literal["register", "flag", "climate_mode", "climate_preset"]


@dataclass(frozen=True, slots=True)
class Command:
    kind: CommandKind
    key: str
    flag: str | None = None


class Topics:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    @property
    def status(self) -> str:
        return f"{self.prefix}/status"

    @property
    def boiler_status(self) -> str:
        return f"{self.prefix}/boiler_status"

    @property
    def set_result(self) -> str:
        return f"{self.prefix}/set_result"

    def state(self, key: str) -> str:
        return f"{self.prefix}/{key}"

    def command(self, key: str) -> str:
        return f"{self.prefix}/{key}/set"

    def attributes(self, key: str) -> str:
        return f"{self.prefix}/{key}/attributes"

    def flag_state(self, key: str, flag: str) -> str:
        return f"{self.prefix}/{key}/{flag}"

    def flag_command(self, key: str, flag: str) -> str:
        return f"{self.prefix}/{key}/{flag}/set"

    def climate_mode_state(self, key: str) -> str:
        return f"{self.prefix}/climate/{key}/mode"

    def climate_mode_command(self, key: str) -> str:
        return f"{self.prefix}/climate/{key}/mode/set"

    def climate_preset_state(self, key: str) -> str:
        return f"{self.prefix}/climate/{key}/preset"

    def climate_preset_command(self, key: str) -> str:
        return f"{self.prefix}/climate/{key}/preset/set"

    @property
    def command_filters(self) -> list[str]:
        return [
            f"{self.prefix}/+/set",
            f"{self.prefix}/+/+/set",
            f"{self.prefix}/climate/+/+/set",
        ]

    def parse_command(self, topic: str) -> Command | None:
        """Interpret a topic received on one of the command filters; None if it is not ours."""
        head = f"{self.prefix}/"
        if not topic.startswith(head) or not topic.endswith("/set"):
            return None
        parts = topic[len(head) : -len("/set")].split("/")
        if any(not part for part in parts):
            return None
        match parts:
            case [key]:
                return Command("register", key)
            case ["climate", key, "mode"]:
                return Command("climate_mode", key)
            case ["climate", key, "preset"]:
                return Command("climate_preset", key)
            case [key, flag] if key != "climate":
                return Command("flag", key, flag)
        return None
