from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import SettingsConfigDict

from diematic.config.settings import LayeredSettings

_FORBIDDEN_TOPIC_CHARS = set("+#\x00 ")


def _clean_topic(value: str) -> str:
    value = value.strip("/")
    if not value or _FORBIDDEN_TOPIC_CHARS & set(value) or "//" in value:
        raise ValueError("must be a topic path without wildcards, spaces or empty levels")
    return value


class MqttSettings(LayeredSettings):
    model_config = SettingsConfigDict(env_prefix="MQTT_", extra="forbid")

    host: str = Field(default="localhost", min_length=1)
    port: int = Field(default=1883, ge=1, le=65535)
    username: str | None = None
    password: SecretStr | None = None
    client_id: str = Field(default="diematic", min_length=1)
    tls: bool = False
    tls_ca: Path | None = None
    tls_insecure: bool = False
    keepalive: int = Field(default=60, ge=5, le=3600)
    qos: Literal[0, 1, 2] = 1
    topic_prefix: str = "diematic"
    boiler_availability: bool = False
    discovery: bool = True
    discovery_prefix: str = "homeassistant"
    node_id: str = Field(default="diematic", pattern=r"^[A-Za-z0-9_-]+$")
    device_name: str = Field(default="Diematic boiler", min_length=1)

    @field_validator("topic_prefix", "discovery_prefix")
    @classmethod
    def _topic(cls, value: str) -> str:
        return _clean_topic(value)
