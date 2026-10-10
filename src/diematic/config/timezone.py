"""Which time zone the boiler's clock is in.

The boiler stores no zone, so it has to come from somewhere. In order: the configured value, the
TZ environment variable (which Docker and Home Assistant set), the time zone of the machine and
finally UTC, which is only right for a boiler set to UTC and is reported as a warning.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOCALTIME = Path("/etc/localtime")
TIMEZONE_FILE = Path("/etc/timezone")

CONFIGURED = "configuration"
ENVIRONMENT = "the TZ environment variable"
SYSTEM = "the time zone of this machine"
DEFAULT = "default"


@dataclass(frozen=True, slots=True)
class ResolvedTimezone:
    zone: ZoneInfo
    name: str
    source: str

    @property
    def is_fallback(self) -> bool:
        return self.source == DEFAULT


def _load(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name.lstrip(":").strip())
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def _system_name(localtime: Path, timezone_file: Path) -> str | None:
    try:
        target = os.readlink(localtime)
    except OSError:
        target = ""
    marker = "zoneinfo/"
    if marker in target:
        return target.split(marker, 1)[1]
    try:
        return timezone_file.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def resolve_timezone(
    configured: str | None = None,
    environment: Mapping[str, str] | None = None,
    localtime: Path | None = None,
    timezone_file: Path | None = None,
) -> ResolvedTimezone:
    env = os.environ if environment is None else environment
    candidates = (
        (CONFIGURED, configured),
        (ENVIRONMENT, env.get("TZ")),
        (SYSTEM, _system_name(localtime or LOCALTIME, timezone_file or TIMEZONE_FILE)),
    )
    for source, name in candidates:
        zone = _load(name)
        if zone is not None:
            return ResolvedTimezone(zone, zone.key, source)
    return ResolvedTimezone(ZoneInfo("UTC"), "UTC", DEFAULT)
