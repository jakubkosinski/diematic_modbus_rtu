"""Plausible initial register contents for the simulator, derived from a register map."""

from __future__ import annotations

from diematic.registers.loader import RegisterMap
from diematic.registers.model import RegisterDef

DEFAULT_TEMPERATURE = 21.0
DEFAULT_PROGRAM = "07:00-20:00"


def _initial_words(register: RegisterDef) -> list[int]:
    codec = register.codec
    match register.type:
        case "temp":
            low, high = register.min, register.max
            value = (
                (low + high) / 2 if low is not None and high is not None else DEFAULT_TEMPERATURE
            )
            return codec.encode(value)
        case "uint":
            return codec.encode(register.min or 0)
        case "enum":
            return codec.encode(next(iter((register.values or {}).values())))
        case "time_program":
            return codec.encode(DEFAULT_PROGRAM)
        case _:
            return [0] * register.width


def default_registers(regmap: RegisterMap) -> dict[int, int]:
    memory: dict[int, int] = {}
    for register in regmap.registers:
        for offset, word in enumerate(_initial_words(register)):
            memory[register.address + offset] = word
    return memory
