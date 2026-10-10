import asyncio

import pytest

from diematic.modbus import rtu
from diematic.modbus.rtu import FrameKind, FrameSplitter
from diematic.sim.boiler import BoilerSimulator
from diematic.transport.tcp import TcpTransport


class Client:
    def __init__(self, transport):
        self.transport = transport
        self.splitter = FrameSplitter()

    async def exchange(self, request, wait=0.5):
        """Send a request and return the first frame answering it (None if no answer)."""
        await self.transport.write(request)
        loop = asyncio.get_running_loop()
        end = loop.time() + wait
        while loop.time() < end:
            for frame in self.splitter.feed(await self.transport.read(max_wait=0.05)):
                if frame.kind is not FrameKind.READ_REQUEST:
                    return frame
        return None


@pytest.fixture
async def simulator():
    sim = BoilerSimulator(
        registers={10: 100, 11: 0x8005, 12: 7},
        master_phase=0.3,
        silent_phase=0.6,
        master_frame_interval=0.05,
    )
    port = await sim.start()
    transport = TcpTransport("127.0.0.1", port)
    await transport.open()
    yield sim, Client(transport)
    await transport.close()
    await sim.stop()


async def test_master_traffic_precedes_the_silent_phase(simulator):
    _, client = simulator
    splitter = FrameSplitter()
    frames = splitter.feed(await client.transport.read(max_wait=1))
    assert frames and frames[0].kind is FrameKind.READ_REQUEST
    assert frames[0].address != 10


async def test_read_is_answered_in_the_silent_phase(simulator):
    sim, client = simulator
    await sim.wait_for_phase("silent")
    frame = await client.exchange(rtu.build_read_request(10, 10, 3))
    assert frame is not None and frame.values == (100, 0x8005, 7)


async def test_write_updates_registers_and_is_acknowledged(simulator):
    sim, client = simulator
    await sim.wait_for_phase("silent")
    frame = await client.exchange(rtu.build_write_request(10, 12, [42]))
    assert frame.kind is FrameKind.WRITE_RESPONSE
    assert sim.registers[12] == 42 and sim.writes == [(12, (42,))]


async def test_request_during_master_phase_is_a_collision(simulator):
    sim, client = simulator
    await sim.wait_for_phase("master")
    await client.transport.write(rtu.build_read_request(10, 10, 1))
    await sim.wait_for_phase("silent")
    assert sim.collisions == 1 and sim.handled == []


async def test_unknown_address_gives_illegal_address_exception(simulator):
    sim, client = simulator
    await sim.wait_for_phase("silent")
    frame = await client.exchange(rtu.build_read_request(10, 500, 1))
    assert frame.kind is FrameKind.EXCEPTION
    assert frame.exception_code == rtu.EXC_ILLEGAL_ADDRESS


async def test_requests_for_other_slaves_are_ignored(simulator):
    sim, client = simulator
    await sim.wait_for_phase("silent")
    assert await client.exchange(rtu.build_read_request(11, 10, 1), wait=0.2) is None


async def test_written_register_reports_pending_before_settling(simulator):
    sim, client = simulator
    sim.settle_reads = 3
    await sim.wait_for_phase("silent")
    await client.exchange(rtu.build_write_request(10, 12, [9]))
    codes = []
    for _ in range(4):
        frame = await client.exchange(rtu.build_read_request(10, 12, 1))
        codes.append(frame.exception_code if frame.kind is FrameKind.EXCEPTION else frame.values)
    assert codes == [rtu.EXC_WRITE_PENDING, rtu.EXC_WRITE_PENDING, rtu.EXC_TARGET_NO_RESPONSE, (9,)]


async def test_dropped_responses_are_swallowed(simulator):
    sim, client = simulator
    sim.drop_responses = 1
    await sim.wait_for_phase("silent")
    assert await client.exchange(rtu.build_read_request(10, 10, 1), wait=0.15) is None
    assert await client.exchange(rtu.build_read_request(10, 10, 1)) is not None
