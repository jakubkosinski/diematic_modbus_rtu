"""Periodic register reads, decoding and availability tracking."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Protocol

from diematic.core.events import AvailabilityChanged, EventBus
from diematic.core.state import StateStore
from diematic.modbus.rtu import (
    EXC_ILLEGAL_ADDRESS,
    EXC_PATH_UNAVAILABLE,
    EXC_TARGET_NO_RESPONSE,
    ModbusError,
    ModbusException,
)
from diematic.registers.loader import RegisterMap
from diematic.registers.planner import Block, plan_blocks

logger = logging.getLogger(__name__)

_GROUPS = ("fast", "slow")
_UNSUPPORTED_RETRY = 3600.0
# The gateway answers these when it has lost the boiler, so they count as a failed link.
_LINK_LOST_CODES = frozenset({EXC_PATH_UNAVAILABLE, EXC_TARGET_NO_RESPONSE})


class RegisterBus(Protocol):
    async def read_registers(self, start: int, count: int, *, max_wait: float) -> list[int]: ...

    async def write_registers(self, start: int, values: list[int], *, max_wait: float) -> None: ...


@dataclass(frozen=True, slots=True)
class PollIntervals:
    fast: float = 30.0
    slow: float = 300.0
    retry: float = 10.0


class Poller:
    def __init__(
        self,
        bus: RegisterBus,
        regmap: RegisterMap,
        state: StateStore,
        events: EventBus,
        intervals: PollIntervals | None = None,
        offline_after: int = 3,
        request_wait: float = 60.0,
    ) -> None:
        self._bus = bus
        self._regmap = regmap
        self._state = state
        self._events = events
        self._intervals = intervals or PollIntervals()
        self._offline_after = offline_after
        self._request_wait = request_wait
        self._max_gap = regmap.max_gap
        self._unsupported: dict[str, float] = {}
        self._failures = 0
        self.online: bool | None = None

    async def run(self) -> None:
        due = dict.fromkeys(_GROUPS, 0.0)
        while True:
            for group in _GROUPS:
                if time.monotonic() >= due[group]:
                    healthy = await self.poll_group(group)
                    interval = getattr(self._intervals, group)
                    due[group] = time.monotonic() + (interval if healthy else self._intervals.retry)
            await asyncio.sleep(max(min(due.values()) - time.monotonic(), 0.1))

    async def poll_group(self, group: str) -> bool:
        """Read every block of a poll group once. Returns False if the link misbehaved."""
        healthy = True
        queue = deque(self._plan(group))
        while queue:
            replacements, ok = await self._read_block(queue.popleft())
            healthy = healthy and ok
            queue.extendleft(reversed(replacements))
        return healthy

    def _plan(self, group: str) -> list[Block]:
        now = time.monotonic()
        registers = [
            r
            for r in self._regmap.registers
            if r.poll_group == group and self._unsupported.get(r.key, 0.0) <= now
        ]
        return plan_blocks(registers, self._regmap.max_block, self._max_gap)

    async def _read_block(self, block: Block) -> tuple[list[Block], bool]:
        try:
            words = await self._bus.read_registers(
                block.start, block.count, max_wait=self._request_wait
            )
        except ModbusException as exc:
            self._link(exc.code not in _LINK_LOST_CODES)
            if exc.code == EXC_ILLEGAL_ADDRESS:
                return self._narrow(block), True
            logger.warning("registers %d-%d: %s", block.start, block.end - 1, exc)
            return [], exc.code not in _LINK_LOST_CODES
        except ModbusError as exc:
            self._link(False)
            logger.warning("reading registers %d-%d failed: %s", block.start, block.end - 1, exc)
            return [], False
        self._link(True)
        for register in block.registers:
            offset = register.address - block.start
            for event in self._state.ingest(register, words[offset : offset + register.width]):
                self._events.publish(event)
        return [], True

    def _narrow(self, block: Block) -> list[Block]:
        """React to the gateway rejecting a block: read less per request."""
        if block.count > sum(r.width for r in block.registers):
            self._max_gap = 0
            logger.info("device rejects reads over unmapped registers; reading without gaps")
            return block.split()
        if len(block.registers) > 1:
            return [Block(r.address, r.width, (r,)) for r in block.registers]
        register = block.registers[0]
        self._unsupported[register.key] = time.monotonic() + _UNSUPPORTED_RETRY
        logger.info(
            "register %s (%d) is not supported by this device; skipping it",
            register.key,
            block.start,
        )
        return []

    def _link(self, ok: bool) -> None:
        if ok:
            self._failures = 0
            self._set_online(True)
            return
        self._failures += 1
        if self._failures >= self._offline_after:
            self._set_online(False)

    def _set_online(self, online: bool) -> None:
        if self.online is online:
            return
        self.online = online
        self._state.online = online
        logger.log(
            logging.INFO if online else logging.WARNING,
            "boiler is %s",
            "online" if online else "offline",
        )
        self._events.publish(AvailabilityChanged(online))
