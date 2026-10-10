"""Periodic register reads, decoding and availability tracking."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from diematic.core.events import AvailabilityChanged, EventBus
from diematic.core.state import StateStore
from diematic.modbus.bus import SlaveNotAnswering
from diematic.modbus.rtu import (
    EXC_ILLEGAL_ADDRESS,
    EXC_PATH_UNAVAILABLE,
    EXC_TARGET_NO_RESPONSE,
    FrameError,
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
        offline_after: float = 120.0,
        request_wait: float = 60.0,
        unsupported_after: int = 2,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bus = bus
        self._regmap = regmap
        self._state = state
        self._events = events
        self._intervals = intervals or PollIntervals()
        self._offline_after = offline_after
        self._request_wait = request_wait
        self._unsupported_after = unsupported_after
        self._clock = clock
        self._max_gap = regmap.max_gap
        self._unsupported: dict[str, float] = {}
        self._misses: dict[str, int] = {}
        self._unproven: list[Block] = []
        self._reported: tuple[str, ...] = ()
        self._since = clock()
        self._last_ok: float | None = None
        self.online: bool | None = None

    @property
    def skipped(self) -> list[str]:
        """Keys of the registers currently left out because the boiler does not answer them."""
        now = self._clock()
        return sorted(key for key, until in self._unsupported.items() if until > now)

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
        self._unproven = []
        healthy = await self._drain(deque(self._plan(group)))
        if self._unproven and self._alive():
            # These blocks got no answer before anything had proven the link works; now that
            # other reads have succeeded, find out which of their registers are mute.
            pending, self._unproven = self._unproven, []
            retry = deque(part for block in pending for part in self._isolate(block))
            healthy = await self._drain(retry) and healthy
        self._report_skipped()
        return healthy

    def _report_skipped(self) -> None:
        skipped = tuple(self.skipped)
        if skipped == self._reported:
            return
        self._reported = skipped
        if skipped:
            names = ",".join(skipped)
            logger.info(
                "the boiler does not answer %d registers: %s. To stop polling them for good and "
                "drop their entities set DIEMATIC_DISABLED_REGISTERS=%s",
                len(skipped),
                ", ".join(skipped),
                names,
            )

    async def _drain(self, queue: deque[Block]) -> bool:
        healthy = True
        while queue:
            replacements, ok = await self._read_block(queue.popleft())
            healthy = healthy and ok
            queue.extendleft(reversed(replacements))
        return healthy

    def _plan(self, group: str) -> list[Block]:
        now = self._clock()
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
        except (SlaveNotAnswering, FrameError) as exc:
            logger.warning("reading registers %d-%d failed: %s", block.start, block.end - 1, exc)
            return self._silent(block), False
        except ModbusError as exc:
            self._link(False)
            logger.warning("reading registers %d-%d failed: %s", block.start, block.end - 1, exc)
            return [], False
        self._link(True)
        for register in block.registers:
            self._misses.pop(register.key, None)
            offset = register.address - block.start
            for event in self._state.ingest(register, words[offset : offset + register.width]):
                self._events.publish(event)
        return [], True

    def _silent(self, block: Block) -> list[Block]:
        """A block got no answer. If the link is otherwise fine, find out which register is mute."""
        self._link(False)
        if not self._alive():
            self._unproven.append(block)
            return []
        return self._isolate(block)

    def _isolate(self, block: Block) -> list[Block]:
        if len(block.registers) > 1:
            return [Block(r.address, r.width, (r,)) for r in block.registers]
        register = block.registers[0]
        misses = self._misses.get(register.key, 0) + 1
        if misses < self._unsupported_after:
            self._misses[register.key] = misses
            return []
        self._misses.pop(register.key, None)
        self._unsupported[register.key] = self._clock() + _UNSUPPORTED_RETRY
        logger.info(
            "register %s (%d) does not answer on this boiler; skipping it for an hour",
            register.key,
            block.start,
        )
        return []

    def _alive(self) -> bool:
        return self._last_ok is not None and self._clock() - self._last_ok < self._offline_after

    def _narrow(self, block: Block) -> list[Block]:
        """React to the gateway rejecting a block: read less per request."""
        if block.count > sum(r.width for r in block.registers):
            self._max_gap = 0
            logger.info("device rejects reads over unmapped registers; reading without gaps")
            return block.split()
        if len(block.registers) > 1:
            return [Block(r.address, r.width, (r,)) for r in block.registers]
        register = block.registers[0]
        self._unsupported[register.key] = self._clock() + _UNSUPPORTED_RETRY
        logger.info(
            "register %s (%d) is not supported by this device; skipping it",
            register.key,
            block.start,
        )
        return []

    def _link(self, ok: bool) -> None:
        """Record whether the boiler answered. It goes offline after a stretch without answers."""
        now = self._clock()
        if ok:
            self._last_ok = now
            self._set_online(True)
            return
        reference = self._since if self._last_ok is None else self._last_ok
        if now - reference >= self._offline_after:
            self._set_online(False)

    def _set_online(self, online: bool) -> None:
        if self.online is online:
            return
        self.online = online
        self._state.online = online
        if online:
            logger.info("boiler is online")
        else:
            logger.warning(
                "boiler is offline: no valid answers; check the converter, the slave address "
                "and the bus timing (master_phase must match the boiler's master phase)"
            )
        self._events.publish(AvailabilityChanged(online))
