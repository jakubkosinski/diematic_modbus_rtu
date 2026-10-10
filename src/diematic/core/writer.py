"""Applies set requests: validate, encode, write, then confirm by reading the value back."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from diematic.core.events import EventBus, SetRequest, SetResult
from diematic.core.poller import RegisterBus
from diematic.core.state import StateStore
from diematic.modbus.bus import BusTimeout
from diematic.modbus.rtu import ModbusError, ModbusException
from diematic.registers.codecs import BitsCodec, CodecError
from diematic.registers.loader import RegisterMap
from diematic.registers.model import RegisterDef

logger = logging.getLogger(__name__)

_EPSILON = 1e-9


class WriteRejected(Exception):
    """The request was refused or could not be confirmed; the message is user-facing."""


@dataclass(frozen=True, slots=True)
class WriterOptions:
    read_only: bool = False
    write_attempts: int = 3
    verify_attempts: int = 4
    retry_delay: float = 1.0
    request_wait: float = 60.0


class Writer:
    def __init__(
        self,
        bus: RegisterBus,
        regmap: RegisterMap,
        state: StateStore,
        events: EventBus,
        options: WriterOptions | None = None,
    ) -> None:
        self._bus = bus
        self._regmap = regmap
        self._state = state
        self._events = events
        self._options = options or WriterOptions()

    async def run(self) -> None:
        subscription = self._events.subscribe(SetRequest)
        try:
            while True:
                request = await subscription.get()
                assert isinstance(request, SetRequest)
                self._events.publish(await self.apply(request))
        finally:
            subscription.close()

    async def apply(self, request: SetRequest) -> SetResult:
        """Carry out one request. Failures are reported in the result, never raised."""
        try:
            await self._apply(request)
        except WriteRejected as exc:
            logger.warning("set %s rejected: %s", request.key, exc)
            return SetResult(request.key, False, str(exc))
        except (CodecError, ValueError) as exc:
            logger.warning("set %s: invalid value: %s", request.key, exc)
            return SetResult(request.key, False, f"invalid value: {exc}")
        except ModbusException as exc:
            logger.warning("set %s refused by the device: %s", request.key, exc)
            return SetResult(request.key, False, f"refused by the device: {exc}")
        except ModbusError as exc:
            logger.warning("set %s failed: %s", request.key, exc)
            return SetResult(request.key, False, f"bus error: {exc}")
        except Exception:
            logger.exception("unexpected error while setting %s", request.key)
            return SetResult(request.key, False, "internal error")
        return SetResult(request.key, True)

    async def _apply(self, request: SetRequest) -> None:
        register = self._lookup(request.key)
        bits = request.bits
        if bits is None and register.type == "bits" and isinstance(request.value, dict):
            bits = request.value
        if bits is not None:
            words = await self._flag_words(register, bits)
            if words is None:
                return
        else:
            words = register.codec.encode(self._normalise(register, request.value))
            if register.mask is not None:
                words = await self._keep_ignored_bits(register, words)
        await self._write(register, words)
        await self._confirm(register, words)

    def _lookup(self, key: str) -> RegisterDef:
        if key not in self._regmap:
            raise WriteRejected(f"unknown register {key!r}")
        register = self._regmap.get(key)
        if self._options.read_only:
            raise WriteRejected("writes are disabled (read-only mode)")
        if not register.is_writable:
            raise WriteRejected(f"{key!r} is read-only")
        return register

    def _normalise(self, register: RegisterDef, value: Any) -> Any:
        if register.type not in {"temp", "uint"}:
            return value
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise WriteRejected(f"expected a number, got {value!r}")
        low, high, step = register.min, register.max, register.step
        if (low is not None and value < low - _EPSILON) or (
            high is not None and value > high + _EPSILON
        ):
            raise WriteRejected(f"{value} is outside the allowed range {low}..{high}")
        if step:
            base = low or 0
            value = round(round((value - base) / step) * step + base, 6)
        return value

    async def _flag_words(
        self, register: RegisterDef, changes: dict[str, bool]
    ) -> list[int] | None:
        codec = register.codec
        if not isinstance(codec, BitsCodec) or register.bits is None:
            raise WriteRejected(f"{register.key!r} is not a flags register")
        for name in changes:
            definition = register.bits.get(name)
            if definition is None:
                raise WriteRejected(f"unknown flag {name!r}")
            if not definition.writable:
                raise WriteRejected(f"flag {name!r} is read-only")
        current = await self._read(register)
        updated = codec.apply(current[0], changes)
        if updated == current[0]:
            self._ingest(register, current)
            return None
        return [updated]

    async def _keep_ignored_bits(self, register: RegisterDef, words: list[int]) -> list[int]:
        """A masked register carries other information in the bits we ignore: leave them as is."""
        assert register.mask is not None
        current = await self._read(register)
        return [(current[0] & ~register.mask & 0xFFFF) | words[0]]

    async def _read(self, register: RegisterDef) -> list[int]:
        return await self._bus.read_registers(
            register.address, register.width, max_wait=self._options.request_wait
        )

    async def _write(self, register: RegisterDef, words: list[int]) -> None:
        attempts = self._options.write_attempts
        for attempt in range(1, attempts + 1):
            try:
                await self._bus.write_registers(
                    register.address, words, max_wait=self._options.request_wait
                )
                return
            except ModbusException as exc:
                if not exc.retryable or attempt == attempts:
                    raise
            except BusTimeout:
                if attempt == attempts:
                    raise
            await asyncio.sleep(self._options.retry_delay)

    async def _confirm(self, register: RegisterDef, expected: list[int]) -> None:
        """Read the value back; the gateway reports 'write pending' until it has been applied."""
        seen: list[int] | None = None
        for _ in range(self._options.verify_attempts):
            try:
                seen = await self._read(register)
            except ModbusException as exc:
                if not exc.retryable:
                    raise
            except BusTimeout:
                pass
            else:
                if seen == expected:
                    self._ingest(register, seen)
                    return
            await asyncio.sleep(self._options.retry_delay)
        if seen is not None:
            self._ingest(register, seen)
            raise WriteRejected(f"the device reports {seen} instead of {expected}")
        raise WriteRejected("the written value could not be read back")

    def _ingest(self, register: RegisterDef, words: list[int]) -> None:
        for event in self._state.ingest(register, words):
            self._events.publish(event)
