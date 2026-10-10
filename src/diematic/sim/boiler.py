"""Simulated Diematic boiler behind an RS485-to-TCP converter, for tests and local development.

Mimics the dual-master bus: the boiler talks for `master_phase` seconds, then stays silent for
`silent_phase` seconds and answers requests addressed to it. Requests that arrive while the
boiler is master are ignored and counted as collisions.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

from diematic.modbus import rtu
from diematic.modbus.rtu import Frame, FrameKind, FrameSplitter

logger = logging.getLogger(__name__)

MASTER_PROBE_ADDRESS = 0x20


@dataclass
class BoilerSimulator:
    registers: dict[int, int] = field(default_factory=dict)
    address: int = 10
    master_phase: float = 5.0
    silent_phase: float = 5.0
    master_frame_interval: float = 0.5
    strict_addresses: bool = True
    settle_reads: int = 0
    reject_writes: bool = False
    drop_responses: int = 0
    trailing_zero: bool = True
    collisions: int = 0
    handled: list[Frame] = field(default_factory=list)
    writes: list[tuple[int, tuple[int, ...]]] = field(default_factory=list)
    phase: str = "master"
    _unsettled: dict[int, int] = field(default_factory=dict, repr=False)
    _server: asyncio.Server | None = field(default=None, repr=False)
    _writers: list[asyncio.StreamWriter] = field(default_factory=list, repr=False)
    _phase_changed: asyncio.Condition = field(default_factory=asyncio.Condition, repr=False)

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        self._server = await asyncio.start_server(self._serve, host, port)
        return int(self._server.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        for writer in self._writers:
            writer.close()
        await self._server.wait_closed()
        self._server = None

    async def drop_connections(self) -> None:
        for writer in self._writers:
            writer.close()
        self._writers.clear()

    async def wait_for_phase(self, phase: str) -> None:
        async with self._phase_changed:
            await self._phase_changed.wait_for(lambda: self.phase == phase)

    async def _set_phase(self, phase: str) -> None:
        async with self._phase_changed:
            self.phase = phase
            self._phase_changed.notify_all()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        splitter = FrameSplitter()
        try:
            while True:
                await self._master_phase(reader, writer, splitter)
                await self._silent_phase(reader, writer, splitter)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def _read(self, reader: asyncio.StreamReader, max_wait: float) -> bytes:
        try:
            data = await asyncio.wait_for(reader.read(1024), max(max_wait, 0.001))
        except TimeoutError:
            return b""
        if not data:
            raise ConnectionError("client closed")
        return data

    async def _master_phase(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, splitter: FrameSplitter
    ) -> None:
        loop = asyncio.get_running_loop()
        await self._set_phase("master")
        end = loop.time() + self.master_phase
        next_frame = loop.time()
        probe = rtu.build_read_request(MASTER_PROBE_ADDRESS, 0, 4)
        while (now := loop.time()) < end:
            if now >= next_frame:
                writer.write(probe + b"\x00")
                await writer.drain()
                next_frame += self.master_frame_interval
            data = await self._read(reader, min(0.02, end - now))
            self.collisions += len(splitter.feed(data)) if data else 0

    async def _silent_phase(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, splitter: FrameSplitter
    ) -> None:
        loop = asyncio.get_running_loop()
        await self._set_phase("silent")
        splitter.reset()
        end = loop.time() + self.silent_phase
        while (now := loop.time()) < end:
            data = await self._read(reader, end - now)
            for frame in splitter.feed(data) if data else []:
                response = self._respond(frame)
                if response is None:
                    continue
                if self.drop_responses > 0:
                    self.drop_responses -= 1
                    continue
                writer.write(response + (b"\x00" if self.trailing_zero else b""))
                await writer.drain()

    def _respond(self, frame: Frame) -> bytes | None:
        if frame.address != self.address:
            return None
        self.handled.append(frame)
        if frame.kind is FrameKind.READ_REQUEST:
            return self._read_registers(frame)
        if frame.kind is FrameKind.WRITE_REQUEST:
            return self._write_registers(frame)
        return rtu.build_exception_response(self.address, frame.function, rtu.EXC_ILLEGAL_FUNCTION)

    def _read_registers(self, frame: Frame) -> bytes:
        addresses = range(frame.start, frame.start + frame.count)
        if self.strict_addresses and any(a not in self.registers for a in addresses):
            return rtu.build_exception_response(
                self.address, frame.function, rtu.EXC_ILLEGAL_ADDRESS
            )
        for address in addresses:
            remaining = self._unsettled.get(address, 0)
            if remaining:
                self._unsettled[address] = remaining - 1
                code = rtu.EXC_WRITE_PENDING if remaining > 1 else rtu.EXC_TARGET_NO_RESPONSE
                return rtu.build_exception_response(self.address, frame.function, code)
        return rtu.build_read_response(self.address, [self.registers.get(a, 0) for a in addresses])

    def _write_registers(self, frame: Frame) -> bytes:
        if self.reject_writes:
            return rtu.build_exception_response(self.address, frame.function, rtu.EXC_ILLEGAL_VALUE)
        for offset, value in enumerate(frame.values):
            self.registers[frame.start + offset] = value
            if self.settle_reads:
                self._unsettled[frame.start + offset] = self.settle_reads
        self.writes.append((frame.start, frame.values))
        return rtu.build_write_response(self.address, frame.start, frame.count)
