"""Loads and validates register map files."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from diematic.registers.model import (
    DEFAULT_LANGUAGE,
    ClimateDef,
    DerivedDef,
    MapFile,
    RegisterDef,
)

_DAYS: dict[str, tuple[str, ...]] = {
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "pl": ("Poniedziałek", "Wtorek", "Środa", "Czwartek", "Piątek", "Sobota", "Niedziela"),
}
_DAY_KEYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_PROGRAM_REGISTERS_PER_DAY = 3


class MapError(ValueError):
    """The register map is unreadable or inconsistent."""


@dataclass(frozen=True)
class RegisterMap:
    system: str
    name: str
    slave_address: int
    max_block: int
    max_gap: int
    language: str
    registers: tuple[RegisterDef, ...]
    derived: tuple[DerivedDef, ...]
    composites: tuple[ClimateDef, ...]
    unfiltered: RegisterMap | None = field(default=None, repr=False, compare=False)
    _by_key: dict[str, RegisterDef] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_key", {r.key: r for r in self.registers})

    def get(self, key: str) -> RegisterDef:
        return self._by_key[key]

    def __contains__(self, key: object) -> bool:
        return key in self._by_key

    def excluding(self, names: Iterable[str]) -> RegisterMap:
        """A copy without the named registers.

        A name is a register key, a derived sensor or thermostat key, or a group (such as
        `solar`), which removes every register of that group. Derived sensors and thermostats
        that depend on a removed register go with it. `unfiltered` keeps the original map.
        """
        wanted = list(names)
        if not wanted:
            return self
        groups = {r.group for r in self.registers if r.group}
        groups |= {d.group for d in self.derived if d.group}
        known = {r.key for r in self.registers}
        known |= {d.key for d in self.derived} | {c.key for c in self.composites} | groups
        unknown = sorted(set(wanted) - known)
        if unknown:
            raise MapError(
                f"cannot disable unknown register or group: {', '.join(unknown)} "
                f"(groups: {', '.join(sorted(groups))})"
            )
        chosen = set(wanted)
        registers = tuple(
            r for r in self.registers if r.key not in chosen and r.group not in chosen
        )
        present = {r.key for r in registers}
        derived = tuple(
            d
            for d in self.derived
            if d.key not in chosen
            and d.group not in chosen
            and all(t.key in present for t in d.terms)
        )
        composites = tuple(
            c
            for c in self.composites
            if c.key not in chosen
            and {c.current_temperature, c.low, c.high, c.mode_register} <= present
        )
        return replace(
            self,
            registers=registers,
            derived=derived,
            composites=composites,
            unfiltered=self.unfiltered or self,
        )


def _expand_time_programs(data: MapFile, language: str) -> list[RegisterDef]:
    days = _DAYS.get(language, _DAYS[DEFAULT_LANGUAGE])
    expanded = []
    for program in data.time_programs:
        for index, (day_key, day_name) in enumerate(zip(_DAY_KEYS, days, strict=True)):
            expanded.append(
                RegisterDef(
                    key=f"{program.key}_{day_key}",
                    address=program.address + index * _PROGRAM_REGISTERS_PER_DAY,
                    type="time_program",
                    kind="parameter",
                    name=f"{program.name} - {day_name}",
                    poll="slow",
                    writable=program.writable,
                    group=program.group or program.key,
                )
            )
    return expanded


def _check_consistency(
    registers: list[RegisterDef],
    derived: Iterable[DerivedDef],
    composites: Iterable[ClimateDef] = (),
) -> None:
    keys: set[str] = set()
    for key in [r.key for r in registers] + [d.key for d in derived] + [c.key for c in composites]:
        if key in keys:
            raise MapError(f"duplicate key {key!r}")
        keys.add(key)
    owner: dict[int, str] = {}
    for register in registers:
        for address in range(register.address, register.address + register.width):
            if address in owner:
                raise MapError(
                    f"address {address} used by both {owner[address]!r} and {register.key!r}"
                )
            owner[address] = register.key
    registered = {r.key for r in registers}
    for item in derived:
        for term in item.terms:
            if term.key not in registered:
                raise MapError(f"derived {item.key!r} references unknown register {term.key!r}")
    by_key = {r.key: r for r in registers}
    for climate in composites:
        _check_climate(climate, by_key)


def _check_climate(climate: ClimateDef, registers: dict[str, RegisterDef]) -> None:
    for role in ("current_temperature", "low", "high", "mode_register"):
        if getattr(climate, role) not in registers:
            raise MapError(f"climate {climate.key!r}: unknown register for {role}")
    for role in ("low", "high"):
        setpoint = registers[getattr(climate, role)]
        if not setpoint.is_writable or setpoint.type not in {"temp", "uint"}:
            raise MapError(f"climate {climate.key!r}: {role} must be a writable number")
    mode = registers[climate.mode_register]
    if mode.type != "bits" or mode.bits is None:
        raise MapError(f"climate {climate.key!r}: mode_register must be a flags register")
    flags = [*climate.modes.values(), *climate.presets.values(), *climate.exclusive]
    for flag in flags:
        definition = mode.bits.get(flag)
        if definition is None:
            raise MapError(f"climate {climate.key!r}: unknown flag {flag!r}")
        if not definition.writable:
            raise MapError(f"climate {climate.key!r}: flag {flag!r} is read-only")
    for flag in [*climate.modes.values(), *climate.presets.values()]:
        if flag not in climate.exclusive:
            raise MapError(f"climate {climate.key!r}: flag {flag!r} must be listed in exclusive")


def _read_source(source: str | Path) -> str:
    path = Path(source)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    name = str(source)
    if not name.endswith(".yaml"):
        name += ".yaml"
    resource = resources.files("diematic.maps").joinpath(name)
    if not resource.is_file():
        raise MapError(f"register map {source!r} not found")
    return resource.read_text(encoding="utf-8")


def parse_map(raw: Any, language: str = DEFAULT_LANGUAGE) -> RegisterMap:
    try:
        data = MapFile.model_validate(raw, context={"language": language})
    except ValidationError as exc:
        raise MapError(str(exc)) from exc
    registers = [*data.registers, *_expand_time_programs(data, language)]
    _check_consistency(registers, data.derived, data.composites)
    return RegisterMap(
        system=data.system,
        name=data.name,
        slave_address=data.slave_address,
        max_block=data.max_block,
        max_gap=data.max_gap,
        language=language,
        registers=tuple(sorted(registers, key=lambda r: r.address)),
        derived=tuple(data.derived),
        composites=tuple(data.composites),
    )


def load_map(source: str | Path, language: str = DEFAULT_LANGUAGE) -> RegisterMap:
    """Load a map from a file path, or a built-in map by name (e.g. 'diematic_isystem')."""
    try:
        raw = yaml.safe_load(_read_source(source))
    except yaml.YAMLError as exc:
        raise MapError(f"invalid YAML in {source!r}: {exc}") from exc
    return parse_map(raw, language)
