"""Register map schema. Human-readable text is resolved to a single language while validating."""

from __future__ import annotations

import re
from functools import cached_property
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationInfo,
    model_validator,
)

from diematic.registers.codecs import (
    BitsCodec,
    Codec,
    EnumCodec,
    TempCodec,
    TimeProgramCodec,
    UintCodec,
)

DEFAULT_LANGUAGE = "en"
KEY_PATTERN = r"^[a-z][a-z0-9_]*$"
BIT_PATTERN = KEY_PATTERN


def _localize(value: Any, info: ValidationInfo) -> Any:
    if not isinstance(value, dict):
        return value
    language = (info.context or {}).get("language", DEFAULT_LANGUAGE)
    for candidate in (language, DEFAULT_LANGUAGE):
        if candidate in value:
            return value[candidate]
    raise ValueError(f"no translation for {language!r} or {DEFAULT_LANGUAGE!r}")


Localized = Annotated[str, BeforeValidator(_localize)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BitDef(_Strict):
    bit: int = Field(ge=0, le=15)
    name: Localized
    writable: bool = False
    device_class: str | None = None


class RegisterDef(_Strict):
    key: str = Field(pattern=KEY_PATTERN)
    address: int = Field(ge=0, le=0xFFFF)
    type: Literal["temp", "uint", "enum", "bits", "time_program"]
    kind: Literal["measure", "parameter"]
    name: Localized
    unit: str | None = None
    scale: float | None = Field(default=None, gt=0)
    min: float | None = None
    max: float | None = None
    step: float | None = Field(default=None, gt=0)
    invalid: list[int] | None = None
    values: dict[int, Localized] | None = None
    bits: dict[str, BitDef] | None = None
    poll: Literal["fast", "slow"] | None = None
    writable: bool | None = None
    device_class: str | None = None
    state_class: str | None = None
    icon: str | None = None
    group: str | None = None
    enabled_by_default: bool = True

    @model_validator(mode="after")
    def _check(self) -> RegisterDef:
        if self.type == "enum" and not self.values:
            raise ValueError("enum registers need 'values'")
        if self.type == "bits" and not self.bits:
            raise ValueError("bits registers need 'bits'")
        if self.type != "enum" and self.values is not None:
            raise ValueError("'values' is only valid for enum registers")
        if self.type != "bits" and self.bits is not None:
            raise ValueError("'bits' is only valid for bits registers")
        if self.kind == "measure" and self.writable:
            raise ValueError("measures are read-only")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min must not exceed max")
        if self.bits:
            positions = [b.bit for b in self.bits.values()]
            if len(set(positions)) != len(positions):
                raise ValueError("bit positions must be unique")
            for name in self.bits:
                if not re.match(BIT_PATTERN, name):
                    raise ValueError(f"invalid bit name {name!r}")
        if self.values and any(not 0 <= raw <= 0xFFFF for raw in self.values):
            raise ValueError("enum raw values must fit in 16 bits")
        return self

    @property
    def width(self) -> int:
        return 3 if self.type == "time_program" else 1

    @property
    def is_writable(self) -> bool:
        return self.kind == "parameter" if self.writable is None else self.writable

    @property
    def poll_group(self) -> Literal["fast", "slow"]:
        return self.poll or ("fast" if self.kind == "measure" else "slow")

    @cached_property
    def codec(self) -> Codec:
        match self.type:
            case "temp":
                invalid = [0xFFFF] if self.invalid is None else self.invalid
                return TempCodec(scale=self.scale or 0.1, invalid=frozenset(invalid))
            case "uint":
                return UintCodec(scale=self.scale or 1, invalid=frozenset(self.invalid or []))
            case "enum":
                return EnumCodec(dict(self.values or {}))
            case "bits":
                return BitsCodec({name: b.bit for name, b in (self.bits or {}).items()})
            case _:
                return TimeProgramCodec()


class DerivedTerm(_Strict):
    key: str
    factor: float = 1


class DerivedDef(_Strict):
    """Sensor computed as a weighted sum of other registers (e.g. split counters)."""

    key: str = Field(pattern=KEY_PATTERN)
    name: Localized
    terms: list[DerivedTerm] = Field(min_length=1)
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    group: str | None = None
    enabled_by_default: bool = True


class TimeProgramDef(_Strict):
    """Weekly program stored as 7 consecutive days of 3 registers each."""

    key: str = Field(pattern=KEY_PATTERN)
    name: Localized
    address: int = Field(ge=0, le=0xFFFF)
    writable: bool = True
    group: str | None = None


class ClimateDef(_Strict):
    """A heating circuit presented as a thermostat.

    Modes and presets name flags of `mode_register`; setting one clears the other flags listed
    in `exclusive`, leaving unrelated flags (e.g. DHW bits) untouched.
    """

    key: str = Field(pattern=KEY_PATTERN)
    name: Localized
    current_temperature: str
    low: str
    high: str
    mode_register: str
    modes: dict[Literal["auto", "heat", "off"], str] = Field(min_length=1)
    presets: dict[str, str] = {}
    exclusive: list[str] = Field(min_length=1)
    enabled_by_default: bool = True


class MapFile(_Strict):
    system: str = Field(pattern=KEY_PATTERN)
    name: str
    slave_address: int = Field(default=10, ge=1, le=247)
    max_block: int = Field(default=40, ge=1, le=125)
    max_gap: int = Field(default=0, ge=0)
    templates: dict[str, Any] = {}
    registers: list[RegisterDef] = []
    time_programs: list[TimeProgramDef] = []
    derived: list[DerivedDef] = []
    composites: list[ClimateDef] = []
