"""Last known register values, change detection and derived sensors."""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from diematic.core.events import ValueChanged
from diematic.registers.loader import RegisterMap
from diematic.registers.model import DerivedDef, RegisterDef


@dataclass(slots=True)
class Entry:
    value: Any
    raw: tuple[int, ...]
    updated: float


class StateStore:
    def __init__(self, regmap: RegisterMap) -> None:
        self._regmap = regmap
        self._entries: dict[str, Entry] = {}
        self._dependents: dict[str, list[DerivedDef]] = defaultdict(list)
        for derived in regmap.derived:
            for term in derived.terms:
                self._dependents[term.key].append(derived)

    def value(self, key: str) -> Any:
        entry = self._entries.get(key)
        return entry.value if entry else None

    def raw(self, key: str) -> tuple[int, ...] | None:
        entry = self._entries.get(key)
        return entry.raw if entry else None

    def known(self, key: str) -> bool:
        return key in self._entries

    def snapshot(self) -> list[ValueChanged]:
        return [ValueChanged(key, e.value, e.raw) for key, e in self._entries.items()]

    def ingest(self, register: RegisterDef, words: Sequence[int]) -> list[ValueChanged]:
        """Store freshly read words; returns the events for everything that changed."""
        raw = tuple(words)
        value = register.codec.decode(raw)
        events = self._store(register.key, value, raw)
        for derived in self._dependents.get(register.key, ()) if events else ():
            events.extend(self._recompute(derived))
        return events

    def _store(self, key: str, value: Any, raw: tuple[int, ...]) -> list[ValueChanged]:
        now = time.monotonic()
        entry = self._entries.get(key)
        if entry is not None and entry.value == value and entry.raw == raw:
            entry.updated = now
            return []
        self._entries[key] = Entry(value, raw, now)
        return [ValueChanged(key, value, raw)]

    def _recompute(self, derived: DerivedDef) -> list[ValueChanged]:
        total: float = 0
        for term in derived.terms:
            entry = self._entries.get(term.key)
            if entry is None:
                return []
            if entry.value is None:
                return self._store(derived.key, None, ())
            total += entry.value * term.factor
        integral = all(float(t.factor).is_integer() for t in derived.terms)
        value: float | int = round(total) if integral else round(total, 3)
        return self._store(derived.key, value, ())
