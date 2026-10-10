import asyncio
import contextlib
import socket

import pytest

from diematic.modbus import rtu
from diematic.modbus.bus import (
    BusError,
    BusState,
    BusTimeout,
    BusTiming,
    BusUnavailable,
    DiematicBus,
)
from diematic.modbus.rtu import FrameKind, ModbusException
from diematic.sim.boiler import BoilerSimulator
from diematic.transport.tcp import TcpTransport

REGISTERS = {10: 100, 11: 0x8005, 12: 7, 13: 8}

FAST = BusTiming(
    silence_gap=0.1,
    master_phase=0.25,
    cycle=0.9,
    cycle_guard=0.05,
    window=0.5,
    idle_open_after=0.7,
    response_timeout=0.3,
    min_request_time=0.05,
    stale_frame_after=0.02,
    tick=0.02,
)


class Rig:
    def __init__(self, simulator, timing=FAST):
        self.sim = simulator
        self.timing = timing
        self.bus = None
        self._task = None

    async def start(self):
        port = await self.sim.start()
        self.bus = DiematicBus(TcpTransport("127.0.0.1", port), 10, self.timing)
        self._task = asyncio.create_task(self.bus.run())
        await asyncio.wait_for(self.bus.connected.wait(), 2)
        return self

    async def stop(self):
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        await self.sim.stop()


def make_sim(**overrides):
    options = {
        "registers": dict(REGISTERS),
        "master_phase": 0.3,
        "silent_phase": 0.6,
        "master_frame_interval": 0.05,
    }
    return BoilerSimulator(**{**options, **overrides})


@pytest.fixture
async def rig():
    started = await Rig(make_sim()).start()
    yield started
    await started.stop()


async def test_read_returns_register_values_without_collisions(rig):
    assert await rig.bus.read_registers(10, 3) == [100, 0x8005, 7]
    assert rig.sim.collisions == 0
    assert rig.bus.stats.requests_ok == 1 and rig.bus.stats.windows >= 1


async def test_write_is_applied_and_acknowledged(rig):
    await rig.bus.write_registers(12, [99])
    assert rig.sim.registers[12] == 99
    assert rig.sim.collisions == 0


async def test_writes_are_served_before_reads_queued_earlier(rig):
    await asyncio.gather(
        rig.bus.read_registers(10, 1),
        rig.bus.read_registers(11, 1),
        rig.bus.write_registers(12, [5]),
    )
    assert rig.sim.handled[0].kind is FrameKind.WRITE_REQUEST
    assert [f.kind for f in rig.sim.handled[1:]] == [FrameKind.READ_REQUEST] * 2


async def test_requests_across_several_cycles_never_collide(rig):
    for register in (10, 11, 12):
        assert await rig.bus.read_registers(register, 1) == [REGISTERS[register]]
    assert rig.sim.collisions == 0
    assert rig.bus.stats.windows >= 2


async def test_modbus_exception_is_raised_to_the_caller(rig):
    with pytest.raises(ModbusException) as info:
        await rig.bus.read_registers(500, 1)
    assert info.value.code == rtu.EXC_ILLEGAL_ADDRESS
    assert await rig.bus.read_registers(10, 1) == [100]


async def test_lost_response_times_out_and_later_requests_still_work(rig):
    rig.sim.drop_responses = 1
    with pytest.raises(BusTimeout):
        await rig.bus.read_registers(10, 1)
    assert rig.bus.stats.timeouts == 1
    assert await rig.bus.read_registers(10, 1) == [100]


async def test_request_gives_up_when_no_window_opens_in_time(rig):
    with pytest.raises(BusTimeout):
        await rig.bus.read_registers(10, 1, max_wait=0.01)


async def test_state_is_listening_between_windows(rig):
    assert rig.bus.state is BusState.LISTENING


async def test_boiler_resuming_midway_interrupts_the_window_and_retries():
    timing = BusTiming(
        silence_gap=0.1,
        master_phase=0.25,
        cycle=100,
        cycle_guard=0.05,
        window=2.0,
        idle_open_after=0.7,
        response_timeout=1.0,
        min_request_time=0.05,
        stale_frame_after=0.02,
        tick=0.02,
    )
    rig = await Rig(make_sim(silent_phase=0.3, drop_responses=1), timing).start()
    try:
        assert await rig.bus.read_registers(10, 1, max_wait=5) == [100]
        assert rig.bus.stats.interrupted >= 1
        assert rig.sim.collisions == 0
    finally:
        await rig.stop()


async def test_window_opens_on_a_quiet_bus_after_the_idle_period():
    rig = await Rig(make_sim(master_phase=0, silent_phase=30)).start()
    try:
        assert await rig.bus.read_registers(10, 1) == [100]
    finally:
        await rig.stop()


async def test_requests_fail_fast_while_the_gateway_is_unreachable():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    bus = DiematicBus(TcpTransport("127.0.0.1", port, connect_timeout=0.2), 10, FAST)
    task = asyncio.create_task(bus.run())
    try:
        await asyncio.sleep(0.1)
        with pytest.raises(BusUnavailable):
            await bus.read_registers(10, 1)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_bus_reconnects_after_the_gateway_drops_the_link(rig):
    assert await rig.bus.read_registers(10, 1) == [100]
    await rig.sim.drop_connections()
    for _ in range(100):
        try:
            assert await rig.bus.read_registers(10, 1, max_wait=3) == [100]
            break
        except BusError:
            await asyncio.sleep(0.05)
    else:
        pytest.fail("the bus did not recover")
    assert rig.bus.stats.reconnects >= 2


async def test_pending_requests_fail_when_the_link_drops(rig):
    request = asyncio.create_task(rig.bus.read_registers(10, 1, max_wait=5))
    await asyncio.sleep(0)
    await rig.sim.drop_connections()
    with pytest.raises(BusUnavailable):
        await request
