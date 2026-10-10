"""Conversions between raw 16-bit register words and engineering values."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

Value = float | int | str | dict[str, bool] | None

UNAVAILABLE = 0xFFFF
SIGN_BIT = 0x8000


class CodecError(ValueError):
    """A value cannot be represented in the target register(s)."""


class Codec(Protocol):
    @property
    def width(self) -> int: ...

    def decode(self, words: Sequence[int]) -> Value: ...

    def encode(self, value: Value) -> list[int]: ...


def _decimals(scale: float) -> int:
    return max(0, -math.floor(math.log10(scale))) if scale < 1 else 0


def _scaled(raw: int, scale: float) -> float | int:
    if scale == 1:
        return raw
    return round(raw * scale, _decimals(scale))


def _to_number(value: Value) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise CodecError(f"expected a number, got {value!r}")
    if not math.isfinite(value):
        raise CodecError("value must be finite")
    return float(value)


@dataclass(frozen=True, slots=True)
class TempCodec:
    """Sign-magnitude number: bit 15 is the sign, the low 15 bits the magnitude."""

    scale: float = 0.1
    invalid: frozenset[int] = frozenset({UNAVAILABLE})
    width: int = 1

    def decode(self, words: Sequence[int]) -> Value:
        raw = words[0]
        if raw in self.invalid:
            return None
        magnitude = _scaled(raw & ~SIGN_BIT & 0xFFFF, self.scale)
        return -magnitude if raw & SIGN_BIT and magnitude else magnitude

    def encode(self, value: Value) -> list[int]:
        number = _to_number(value)
        magnitude = round(abs(number) / self.scale)
        if magnitude > 0x7FFF:
            raise CodecError(f"{value} is out of range")
        raw = magnitude | (SIGN_BIT if number < 0 and magnitude else 0)
        return [raw]


@dataclass(frozen=True, slots=True)
class UintCodec:
    scale: float = 1
    invalid: frozenset[int] = frozenset()
    width: int = 1

    def decode(self, words: Sequence[int]) -> Value:
        return None if words[0] in self.invalid else _scaled(words[0], self.scale)

    def encode(self, value: Value) -> list[int]:
        raw = round(_to_number(value) / self.scale)
        if not 0 <= raw <= 0xFFFF:
            raise CodecError(f"{value} is out of range")
        return [raw]


@dataclass(frozen=True, slots=True)
class EnumCodec:
    options: Mapping[int, str]
    width: int = 1
    _by_label: dict[str, int] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_label", {label: raw for raw, label in self.options.items()})

    def decode(self, words: Sequence[int]) -> Value:
        return self.options.get(words[0])

    def encode(self, value: Value) -> list[int]:
        if isinstance(value, str) and value in self._by_label:
            return [self._by_label[value]]
        raise CodecError(f"{value!r} is not one of {sorted(self._by_label)}")


@dataclass(frozen=True, slots=True)
class BitsCodec:
    """Register used as a set of named flags. Decodes to {name: bool}."""

    bits: Mapping[str, int]
    width: int = 1

    def decode(self, words: Sequence[int]) -> Value:
        return {name: bool(words[0] >> bit & 1) for name, bit in self.bits.items()}

    def encode(self, value: Value) -> list[int]:
        if not isinstance(value, dict):
            raise CodecError("expected a mapping of flag names to booleans")
        return [self.apply(0, value)]

    def apply(self, current: int, changes: Mapping[str, bool]) -> int:
        """Return `current` with only the named flags changed (read-modify-write)."""
        for name, state in changes.items():
            if name not in self.bits:
                raise CodecError(f"unknown flag {name!r}")
            mask = 1 << self.bits[name]
            current = current | mask if state else current & ~mask
        return current & 0xFFFF


_SLOT_MINUTES = 30
_SLOTS = 48
_RANGE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")


def _fmt(slot: int) -> str:
    minutes = slot * _SLOT_MINUTES
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _slot(hours: str, minutes: str) -> int:
    total = int(hours) * 60 + int(minutes)
    if total % _SLOT_MINUTES or total > _SLOTS * _SLOT_MINUTES:
        raise CodecError(f"time {hours}:{minutes} is not on a 30 minute boundary within the day")
    return total // _SLOT_MINUTES


@dataclass(frozen=True, slots=True)
class TimeProgramCodec:
    """One day of a weekly program: 3 words, one bit per half hour, MSB of the first word first.

    A set bit means the day/comfort period. Values are text such as "07:00-20:00,22:00-23:30".
    """

    width: int = 3

    def decode(self, words: Sequence[int]) -> Value:
        bits = [(words[i // 16] >> (15 - i % 16)) & 1 for i in range(_SLOTS)]
        periods: list[str] = []
        start: int | None = None
        for slot, bit in enumerate([*bits, 0]):
            if bit and start is None:
                start = slot
            elif not bit and start is not None:
                periods.append(f"{_fmt(start)}-{_fmt(slot)}")
                start = None
        return ",".join(periods)

    def encode(self, value: Value) -> list[int]:
        if not isinstance(value, str):
            raise CodecError("expected text such as '07:00-20:00'")
        bits = [0] * _SLOTS
        for part in filter(None, (p.strip() for p in value.split(","))):
            match = _RANGE.match(part)
            if not match:
                raise CodecError(f"invalid period {part!r}")
            first, last = _slot(*match.group(1, 2)), _slot(*match.group(3, 4))
            if first >= last:
                raise CodecError(f"period {part!r} must end after it starts")
            for slot in range(first, last):
                bits[slot] = 1
        return [
            sum(bit << (15 - i) for i, bit in enumerate(bits[offset : offset + 16]))
            for offset in range(0, _SLOTS, 16)
        ]
