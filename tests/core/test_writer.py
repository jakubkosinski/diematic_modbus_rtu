import asyncio
import contextlib

import pytest

from diematic.core.events import EventBus, SetRequest, SetResult, ValueChanged
from diematic.core.state import StateStore
from diematic.core.writer import Writer, WriterOptions
from diematic.modbus import rtu
from diematic.modbus.bus import BusTimeout, BusUnavailable
from diematic.modbus.rtu import ModbusException
from diematic.registers.loader import parse_map

SETPOINT, MODE, FORCING, SENSOR, PROGRAM = 10, 11, 12, 20, 30
ACTIVE = 60

REGMAP = parse_map(
    {
        "system": "t",
        "name": "t",
        "registers": [
            {
                "key": "setpoint",
                "address": SETPOINT,
                "type": "temp",
                "kind": "parameter",
                "name": "Setpoint",
                "min": 5,
                "max": 30,
                "step": 0.5,
            },
            {
                "key": "mode",
                "address": MODE,
                "type": "enum",
                "kind": "parameter",
                "name": "Mode",
                "values": {0: "off", 1: "on"},
            },
            {
                "key": "forcing",
                "address": FORCING,
                "type": "bits",
                "kind": "parameter",
                "name": "Forcing",
                "bits": {
                    "auto": {"bit": 3, "name": "Auto", "writable": True},
                    "night": {"bit": 1, "name": "Night", "writable": True},
                    "locked": {"bit": 7, "name": "Locked"},
                },
            },
            {"key": "sensor", "address": SENSOR, "type": "temp", "kind": "measure", "name": "S"},
            {
                "key": "active",
                "address": ACTIVE,
                "type": "enum",
                "kind": "parameter",
                "name": "Active program",
                "mask": 3,
                "values": {0: "P1", 1: "P2", 2: "P3", 3: "P4"},
            },
        ],
        "time_programs": [{"key": "prog", "name": "Program", "address": PROGRAM}],
    }
)

FAST = WriterOptions(retry_delay=0, write_attempts=3, verify_attempts=4)


class FakeBus:
    def __init__(self, registers=None):
        self.registers = registers if registers is not None else {SETPOINT: 100, MODE: 0}
        self.registers.setdefault(FORCING, 0)
        self.writes = []
        self.write_script = []
        self.read_script = []
        self.pending_reads = 0
        self.ignore_writes = False

    async def read_registers(self, start, count, *, max_wait=0):
        if self.read_script:
            outcome = self.read_script.pop(0)
            if outcome is not None:
                raise outcome
        if self.pending_reads:
            self.pending_reads -= 1
            raise ModbusException(rtu.FC_READ_HOLDING, rtu.EXC_WRITE_PENDING)
        return [self.registers.get(start + i, 0) for i in range(count)]

    async def write_registers(self, start, values, *, max_wait=0):
        self.writes.append((start, list(values)))
        if self.write_script:
            outcome = self.write_script.pop(0)
            if outcome is not None:
                raise outcome
        if not self.ignore_writes:
            for offset, value in enumerate(values):
                self.registers[start + offset] = value


@pytest.fixture
def rig():
    bus, events = FakeBus(), EventBus()
    state = StateStore(REGMAP)
    subscription = events.subscribe()
    return Writer(bus, REGMAP, state, events, FAST), bus, state, subscription


def published(subscription):
    items = []
    while not subscription.queue.empty():
        items.append(subscription.queue.get_nowait())
    return items


async def test_value_is_written_confirmed_and_published(rig):
    writer, bus, state, events = rig
    result = await writer.apply(SetRequest("setpoint", 21.5))
    assert result == SetResult("setpoint", True)
    assert bus.writes == [(SETPOINT, [215])]
    assert state.value("setpoint") == 21.5
    assert ValueChanged("setpoint", 21.5, (215,)) in published(events)


async def test_value_is_snapped_to_the_step(rig):
    writer, bus, _, _ = rig
    assert (await writer.apply(SetRequest("setpoint", 21.3))).ok
    assert bus.writes == [(SETPOINT, [215])]


@pytest.mark.parametrize("value", [4.5, 30.5, "21", True, None])
async def test_invalid_numbers_are_rejected_without_touching_the_bus(rig, value):
    writer, bus, _, _ = rig
    result = await writer.apply(SetRequest("setpoint", value))
    assert not result.ok and bus.writes == []


async def test_unknown_and_read_only_registers_are_rejected(rig):
    writer, bus, _, _ = rig
    assert "unknown" in (await writer.apply(SetRequest("nope", 1))).message
    assert "read-only" in (await writer.apply(SetRequest("sensor", 1.0))).message
    assert bus.writes == []


async def test_read_only_mode_rejects_everything():
    bus, events = FakeBus(), EventBus()
    writer = Writer(bus, REGMAP, StateStore(REGMAP), events, WriterOptions(read_only=True))
    result = await writer.apply(SetRequest("setpoint", 20))
    assert not result.ok and "read-only mode" in result.message and bus.writes == []


async def test_enum_is_written_by_label(rig):
    writer, bus, _, _ = rig
    assert (await writer.apply(SetRequest("mode", "on"))).ok
    assert bus.writes == [(MODE, [1])]
    assert not (await writer.apply(SetRequest("mode", "turbo"))).ok


async def test_time_program_is_written_across_three_registers(rig):
    writer, bus, state, _ = rig
    assert (await writer.apply(SetRequest("prog_monday", "07:00-20:00"))).ok
    assert bus.writes == [(PROGRAM, [0x0003, 0xFFFF, 0xFF00])]
    assert state.value("prog_monday") == "07:00-20:00"


async def test_flags_are_changed_with_read_modify_write(rig):
    writer, bus, state, _ = rig
    bus.registers[FORCING] = 0b1000_0000 | 0b1000
    result = await writer.apply(SetRequest("forcing", bits={"auto": False, "night": True}))
    assert result.ok
    assert bus.writes == [(FORCING, [0b1000_0000 | 0b0010])]
    assert state.value("forcing")["night"] and state.value("forcing")["locked"]


async def test_flags_dictionary_as_value_means_the_same(rig):
    writer, bus, _, _ = rig
    assert (await writer.apply(SetRequest("forcing", {"night": True}))).ok
    assert bus.writes == [(FORCING, [0b10])]


async def test_flags_already_in_place_are_not_rewritten(rig):
    writer, bus, state, _ = rig
    bus.registers[FORCING] = 0b1000
    assert (await writer.apply(SetRequest("forcing", bits={"auto": True}))).ok
    assert bus.writes == [] and state.value("forcing")["auto"]


@pytest.mark.parametrize("flag", ["locked", "missing"])
async def test_read_only_and_unknown_flags_are_rejected(rig, flag):
    writer, bus, _, _ = rig
    assert not (await writer.apply(SetRequest("forcing", bits={flag: True}))).ok
    assert bus.writes == []


async def test_flags_on_a_non_flag_register_are_rejected(rig):
    writer, _, _, _ = rig
    assert not (await writer.apply(SetRequest("setpoint", bits={"auto": True}))).ok


async def test_transient_device_exception_on_write_is_retried(rig):
    writer, bus, _, _ = rig
    busy = ModbusException(rtu.FC_WRITE_MULTIPLE, rtu.EXC_WRITE_PENDING)
    bus.write_script = [busy, busy]
    assert (await writer.apply(SetRequest("setpoint", 20))).ok
    assert len(bus.writes) == 3


async def test_persistent_transient_exception_eventually_fails(rig):
    writer, bus, _, _ = rig
    busy = ModbusException(rtu.FC_WRITE_MULTIPLE, rtu.EXC_WRITE_PENDING)
    bus.write_script = [busy] * 3
    result = await writer.apply(SetRequest("setpoint", 20))
    assert not result.ok and len(bus.writes) == 3


async def test_non_retryable_device_exception_fails_at_once(rig):
    writer, bus, _, _ = rig
    bus.write_script = [ModbusException(rtu.FC_WRITE_MULTIPLE, rtu.EXC_ILLEGAL_VALUE)]
    result = await writer.apply(SetRequest("setpoint", 20))
    assert not result.ok and "refused" in result.message and len(bus.writes) == 1


async def test_lost_acknowledgement_is_retried_and_confirmed(rig):
    writer, bus, _, _ = rig
    bus.write_script = [BusTimeout("no answer")]
    assert (await writer.apply(SetRequest("setpoint", 20))).ok
    assert len(bus.writes) == 2


async def test_readback_waits_while_the_gateway_reports_the_write_as_pending(rig):
    writer, bus, _, _ = rig
    bus.pending_reads = 2
    assert (await writer.apply(SetRequest("setpoint", 20))).ok


async def test_readback_gives_up_when_the_write_stays_pending(rig):
    writer, bus, _, _ = rig
    bus.pending_reads = 99
    result = await writer.apply(SetRequest("setpoint", 20))
    assert not result.ok and "read back" in result.message


async def test_value_that_does_not_stick_is_reported_and_the_real_value_published(rig):
    writer, bus, state, events = rig
    bus.ignore_writes = True
    result = await writer.apply(SetRequest("setpoint", 20))
    assert not result.ok and "instead of" in result.message
    assert state.value("setpoint") == 10.0
    assert ValueChanged("setpoint", 10.0, (100,)) in published(events)


async def test_unavailable_bus_is_reported_not_raised(rig):
    writer, bus, _, _ = rig
    bus.write_script = [BusUnavailable("down")]
    result = await writer.apply(SetRequest("setpoint", 20))
    assert not result.ok and "bus error" in result.message


async def test_unexpected_errors_are_contained(rig):
    writer, bus, _, _ = rig
    bus.write_script = [RuntimeError("boom")]
    result = await writer.apply(SetRequest("setpoint", 20))
    assert result == SetResult("setpoint", False, "internal error")


async def test_run_serves_requests_from_the_event_bus():
    bus, events = FakeBus(), EventBus()
    writer = Writer(bus, REGMAP, StateStore(REGMAP), events, FAST)
    results = events.subscribe(SetResult)
    task = asyncio.create_task(writer.run())
    await asyncio.sleep(0)
    events.publish(SetRequest("setpoint", 20))
    events.publish(SetRequest("mode", "on"))
    first = await asyncio.wait_for(results.get(), 1)
    second = await asyncio.wait_for(results.get(), 1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert (first, second) == (SetResult("setpoint", True), SetResult("mode", True))


async def test_run_survives_a_failing_request():
    bus, events = FakeBus(), EventBus()
    bus.write_script = [RuntimeError("boom")]
    writer = Writer(bus, REGMAP, StateStore(REGMAP), events, FAST)
    results = events.subscribe(SetResult)
    task = asyncio.create_task(writer.run())
    await asyncio.sleep(0)
    events.publish(SetRequest("setpoint", 20))
    events.publish(SetRequest("setpoint", 21))
    outcomes = [await asyncio.wait_for(results.get(), 1) for _ in range(2)]
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert [o.ok for o in outcomes] == [False, True]


async def test_bits_outside_the_mask_survive_a_write(rig):
    writer, bus, state, _ = rig
    bus.registers[ACTIVE] = 8192
    assert (await writer.apply(SetRequest("active", "P2"))).ok
    assert bus.writes == [(ACTIVE, [8193])]
    assert bus.registers[ACTIVE] == 8193
    assert state.value("active") == "P2" and state.raw("active") == (8193,)


async def test_a_masked_register_without_extra_bits_is_written_plainly(rig):
    writer, bus, _, _ = rig
    bus.registers[ACTIVE] = 0
    assert (await writer.apply(SetRequest("active", "P3"))).ok
    assert bus.writes == [(ACTIVE, [2])]


async def test_registers_without_a_mask_are_written_without_an_extra_read(rig):
    writer, bus, _, _ = rig
    reads = []
    original = bus.read_registers

    async def counting(start, count, *, max_wait=0):
        reads.append(start)
        return await original(start, count, max_wait=max_wait)

    bus.read_registers = counting
    assert (await writer.apply(SetRequest("setpoint", 20))).ok
    assert reads == [SETPOINT]
