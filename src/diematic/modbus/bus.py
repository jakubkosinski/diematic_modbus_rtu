"""Access to the Diematic bus, where the boiler is the master most of the time.

The boiler talks for about 5 s, then stays silent for about 5 s and listens for requests. This
module watches the line, opens a window for our own requests only inside the silent phase and
runs queued requests there, writes before reads. If the boiler starts talking again mid-window we
stop at once and retry in the next one.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import time
from dataclasses import dataclass, field
from enum import Enum

from diematic.modbus import rtu
from diematic.modbus.rtu import Frame, FrameKind, FrameSplitter, ModbusError
from diematic.transport.backoff import Backoff
from diematic.transport.base import Transport

logger = logging.getLogger(__name__)

PRIORITY_WRITE = 0
PRIORITY_READ = 1
_REQUEST_KINDS = frozenset({FrameKind.READ_REQUEST, FrameKind.WRITE_REQUEST})
_STABLE_SESSION = 10.0


class BusError(ModbusError):
    """The request could not be carried out on the bus."""


class BusTimeout(BusError):
    """No window opened in time, or the slave did not answer inside it."""


class BusUnavailable(BusError):
    """The link to the RS485 gateway is down."""


class BusState(Enum):
    DISCONNECTED = "disconnected"
    LISTENING = "listening"
    WINDOW = "window"


@dataclass(frozen=True, slots=True)
class BusTiming:
    silence_gap: float = 0.5
    master_phase: float = 5.0
    cycle: float = 10.0
    cycle_guard: float = 0.5
    window: float = 4.0
    idle_open_after: float = 6.0
    response_timeout: float = 2.5
    min_request_time: float = 0.3
    linger: float = 0.1
    stale_frame_after: float = 0.05
    tick: float = 0.1


@dataclass
class BusStats:
    windows: int = 0
    boiler_frames: int = 0
    requests_ok: int = 0
    requests_failed: int = 0
    timeouts: int = 0
    interrupted: int = 0
    reconnects: int = 0


@dataclass(order=True)
class _Request:
    priority: int
    seq: int
    frame: bytes = field(compare=False)
    is_write: bool = field(compare=False)
    start: int = field(compare=False)
    count: int = field(compare=False)
    future: asyncio.Future[list[int] | None] = field(compare=False)


class DiematicBus:
    def __init__(
        self,
        transport: Transport,
        slave_address: int,
        timing: BusTiming | None = None,
        trailing_zero: bool = True,
    ) -> None:
        self._transport = transport
        self._slave = slave_address
        self._timing = timing or BusTiming()
        self._pad = b"\x00" if trailing_zero else b""
        self._pending: list[_Request] = []
        self._seq = itertools.count()
        self.state = BusState.DISCONNECTED
        self.connected = asyncio.Event()
        self._request_added = asyncio.Event()
        self.stats = BusStats()
        self.last_response: float | None = None

    async def read_registers(self, start: int, count: int, *, max_wait: float = 30.0) -> list[int]:
        frame = rtu.build_read_request(self._slave, start, count)
        result = await self._submit(PRIORITY_READ, frame, False, start, count, max_wait)
        assert result is not None
        return result

    async def write_registers(
        self, start: int, values: list[int], *, max_wait: float = 30.0
    ) -> None:
        frame = rtu.build_write_request(self._slave, start, values)
        await self._submit(PRIORITY_WRITE, frame, True, start, len(values), max_wait)

    async def _submit(
        self, priority: int, frame: bytes, is_write: bool, start: int, count: int, max_wait: float
    ) -> list[int] | None:
        if self.state is BusState.DISCONNECTED:
            raise BusUnavailable("not connected to the RS485 gateway")
        future: asyncio.Future[list[int] | None] = asyncio.get_running_loop().create_future()
        request = _Request(priority, next(self._seq), frame, is_write, start, count, future)
        heapq.heappush(self._pending, request)
        self._request_added.set()
        try:
            return await asyncio.wait_for(future, max_wait)
        except TimeoutError:
            raise BusTimeout("no bus window within the allowed time") from None

    async def run(self) -> None:
        """Keep the link up and serve requests until cancelled. Never raises on I/O errors."""
        backoff = Backoff()
        while True:
            started = time.monotonic()
            try:
                await self._transport.open()
            except OSError as exc:
                logger.warning("cannot connect to the RS485 gateway: %s", exc)
            else:
                self.stats.reconnects += 1
                try:
                    await self._session()
                except OSError as exc:
                    logger.warning("lost connection to the RS485 gateway: %s", exc)
            finally:
                await self._transport.close()
                self.state = BusState.DISCONNECTED
                self.connected.clear()
                self._fail_pending(BusUnavailable("connection to the RS485 gateway lost"))
            if time.monotonic() - started > _STABLE_SESSION:
                backoff.reset()
            await asyncio.sleep(backoff.next_delay())

    def _fail_pending(self, error: BusError) -> None:
        pending, self._pending = self._pending, []
        for request in pending:
            if not request.future.done():
                request.future.set_exception(error)

    async def _session(self) -> None:
        timing = self._timing
        splitter = FrameSplitter()
        self.state = BusState.LISTENING
        self.connected.set()
        last_rx = time.monotonic()
        burst_start: float | None = None
        while True:
            chunk = await self._transport.read(timing.tick)
            now = time.monotonic()
            if chunk:
                burst_start, last_rx = self._note_traffic(burst_start, last_rx, now)
                self.stats.boiler_frames += len(splitter.feed(chunk))
            elif splitter.pending_bytes and now - last_rx >= timing.stale_frame_after:
                splitter.reset()
            deadline = self._window_deadline(now, burst_start, last_rx)
            if deadline is not None:
                interrupted_at = await self._run_window(splitter, deadline)
                last_rx = time.monotonic()
                if interrupted_at is not None:
                    burst_start, last_rx = interrupted_at, interrupted_at

    def _note_traffic(
        self, burst_start: float | None, last_rx: float, now: float
    ) -> tuple[float, float]:
        timing = self._timing
        if burst_start is None or (
            now - last_rx >= timing.silence_gap and now - burst_start >= timing.master_phase
        ):
            burst_start = now
        return burst_start, now

    def _window_deadline(
        self, now: float, burst_start: float | None, last_rx: float
    ) -> float | None:
        """When a window opened now would have to end, or None if none should open."""
        timing = self._timing
        if not self._has_live_request():
            return None
        silence = now - last_rx
        if silence >= timing.idle_open_after:
            return now + timing.window
        if burst_start is None:
            return None
        if now - burst_start < timing.master_phase or silence < timing.silence_gap:
            return None
        deadline = min(now + timing.window, burst_start + timing.cycle - timing.cycle_guard)
        return deadline if deadline - now >= timing.min_request_time else None

    async def _run_window(self, splitter: FrameSplitter, deadline: float) -> float | None:
        """Serve queued requests until the deadline; returns when the boiler resumed talking."""
        timing = self._timing
        self.stats.windows += 1
        self.state = BusState.WINDOW
        splitter.reset()
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining < timing.min_request_time:
                    return None
                request = self._pop_request()
                if request is None:
                    # a caller awaiting replies one by one submits its next request only after
                    # we resume, so give it a moment instead of closing the window
                    if not await self._wait_for_request(min(timing.linger, remaining)):
                        return None
                    continue
                interrupted_at = await self._exchange(
                    request, splitter, min(remaining, timing.response_timeout)
                )
                if interrupted_at is not None:
                    return interrupted_at
        finally:
            self.state = BusState.LISTENING

    async def _wait_for_request(self, limit: float) -> bool:
        self._request_added.clear()
        if self._has_live_request():
            return True
        try:
            await asyncio.wait_for(self._request_added.wait(), limit)
        except TimeoutError:
            return False
        return True

    def _has_live_request(self) -> bool:
        return any(not request.future.done() for request in self._pending)

    def _pop_request(self) -> _Request | None:
        while self._pending:
            request = heapq.heappop(self._pending)
            if not request.future.done():
                return request
        return None

    async def _exchange(
        self, request: _Request, splitter: FrameSplitter, limit: float
    ) -> float | None:
        try:
            await self._transport.write(request.frame + self._pad)
        except OSError:
            self._finish(request, error=BusUnavailable("write to the RS485 gateway failed"))
            raise
        logger.debug("sent %s", request.frame.hex())
        end = time.monotonic() + limit
        while True:
            left = end - time.monotonic()
            if left <= 0:
                self.stats.timeouts += 1
                self._finish(request, error=BusTimeout("the slave did not answer"))
                return None
            try:
                chunk = await self._transport.read(left)
            except OSError:
                self._finish(request, error=BusUnavailable("connection lost during a request"))
                raise
            for frame in splitter.feed(chunk) if chunk else []:
                if frame.raw == request.frame:
                    continue
                if self._answers(request, frame):
                    self._complete(request, frame)
                    return None
                if frame.address != self._slave or frame.kind in _REQUEST_KINDS:
                    self.stats.interrupted += 1
                    heapq.heappush(self._pending, request)
                    return time.monotonic()

    def _answers(self, request: _Request, frame: Frame) -> bool:
        if frame.address != self._slave:
            return False
        expected = FrameKind.WRITE_RESPONSE if request.is_write else FrameKind.READ_RESPONSE
        return frame.kind in {expected, FrameKind.EXCEPTION}

    def _complete(self, request: _Request, frame: Frame) -> None:
        self.last_response = time.monotonic()
        try:
            if request.is_write:
                rtu.check_write_response(frame, self._slave, request.start, request.count)
                result = None
            else:
                result = rtu.parse_read_response(frame, self._slave, request.count)
        except ModbusError as exc:
            self._finish(request, error=exc)
        else:
            self._finish(request, result=result)

    def _finish(
        self,
        request: _Request,
        *,
        result: list[int] | None = None,
        error: ModbusError | None = None,
    ) -> None:
        if error is None:
            self.stats.requests_ok += 1
        else:
            self.stats.requests_failed += 1
        if request.future.done():
            return
        if error is None:
            request.future.set_result(result)
        else:
            request.future.set_exception(error)
