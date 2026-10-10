"""Application settings: environment variables override the YAML file, which overrides defaults."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from diematic.modbus.bus import BusTiming

CONFIG_ENV = "DIEMATIC_CONFIG"
PLUGINS_ENV = "DIEMATIC_ENABLED_PLUGINS"
DEFAULT_PLUGINS = ["mqtt"]
_TOP_LEVEL_KEYS = {"gateway", "plugins"}


class ConfigError(Exception):
    """The configuration is unreadable or invalid. The message never contains secret values."""


def format_validation_error(exc: ValidationError, prefix: str = "") -> str:
    """Render a validation error without echoing the offending input (it may be a password)."""
    lines = []
    for error in exc.errors(include_input=False, include_url=False, include_context=False):
        location = ".".join(str(part) for part in error["loc"])
        lines.append(f"{prefix}{location}: {error['msg']}")
    return "; ".join(lines)


class LayeredSettings(BaseSettings):
    """Base class: values passed in (from the YAML file) rank below environment variables.

    Empty environment variables count as unset, so a blank `KEY=` line in an .env file never
    turns into an empty username or password.
    """

    model_config = SettingsConfigDict(env_ignore_empty=True)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return env_settings, init_settings


class BusSettings(BaseModel):
    """Overrides for the bus timing; unset fields keep the defaults."""

    silence_gap: float | None = Field(default=None, gt=0)
    master_phase: float | None = Field(default=None, gt=0)
    cycle: float | None = Field(default=None, gt=0)
    cycle_guard: float | None = Field(default=None, ge=0)
    window: float | None = Field(default=None, gt=0)
    idle_open_after: float | None = Field(default=None, gt=0)
    response_timeout: float | None = Field(default=None, gt=0)

    def timing(self) -> BusTiming:
        overrides = {k: v for k, v in self.model_dump().items() if v is not None}
        return BusTiming(**overrides)


class GatewaySettings(LayeredSettings):
    model_config = SettingsConfigDict(
        env_prefix="DIEMATIC_", env_nested_delimiter="__", extra="forbid"
    )

    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    slave_address: int | None = Field(default=None, ge=1, le=247)
    map: str = "diematic_isystem"
    disabled_registers: Annotated[list[str], NoDecode] = Field(default_factory=list)
    language: Literal["pl", "en"] = "pl"
    read_only: bool = False
    trailing_zero: bool = True
    connect_timeout: float = Field(default=5.0, gt=0)
    fast_interval: float = Field(default=30.0, ge=1)
    slow_interval: float = Field(default=300.0, ge=1)
    offline_after: float = Field(default=120.0, ge=10)
    clock_sync: bool = False
    timezone: str = "UTC"
    heartbeat_file: Path | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    bus: BusSettings = Field(default_factory=BusSettings)

    @field_validator("disabled_registers", mode="before")
    @classmethod
    def _split_names(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.split(",")
        if isinstance(value, list | tuple):
            return [str(item).strip() for item in value if str(item).strip()]
        return value

    @field_validator("disabled_registers")
    @classmethod
    def _valid_names(cls, names: list[str]) -> list[str]:
        for name in names:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                raise ValueError("register and group names are lower case words with underscores")
        return names

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {value!r}") from exc
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_case_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


@dataclass(frozen=True)
class AppSettings:
    gateway: GatewaySettings
    enabled_plugins: list[str]
    plugin_config: dict[str, dict[str, Any]] = field(default_factory=dict)

    def masked(self) -> dict[str, Any]:
        """Gateway settings for logging. Plugin settings mask their own secrets."""
        return self.gateway.model_dump(mode="json")


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file {path}: {exc.strerror}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"configuration file {path} is not valid YAML") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"configuration file {path} must contain a mapping")
    unknown = set(raw) - _TOP_LEVEL_KEYS
    if unknown:
        raise ConfigError(f"unknown configuration sections: {', '.join(sorted(map(str, unknown)))}")
    return raw


def load_settings(config_path: str | Path | None = None) -> AppSettings:
    path = config_path or os.environ.get(CONFIG_ENV)
    raw = _read_yaml(Path(path)) if path else {}
    gateway_values = raw.get("gateway") or {}
    plugin_sections = raw.get("plugins") or {}
    if not isinstance(gateway_values, dict) or not isinstance(plugin_sections, dict):
        raise ConfigError("'gateway' and 'plugins' must be mappings")
    try:
        gateway = GatewaySettings(**gateway_values)
    except ValidationError as exc:
        raise ConfigError(format_validation_error(exc, "gateway.")) from exc
    listed = os.environ.get(PLUGINS_ENV)
    if listed is not None:
        enabled = [name.strip() for name in listed.split(",") if name.strip()]
    else:
        enabled = [str(name) for name in plugin_sections] or list(DEFAULT_PLUGINS)
    config = {str(name): dict(values or {}) for name, values in plugin_sections.items()}
    return AppSettings(gateway, enabled, config)
