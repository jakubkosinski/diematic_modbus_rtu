import asyncio
import contextlib

import pytest

from diematic.core.events import AvailabilityChanged, EventBus, ValueChanged
from diematic.core.poller import Poller, PollIntervals
from diematic.core.state import StateStore
from diematic.modbus import rtu
from diematic.modbus.bus import BusTimeout, SlaveNotAnswering
from diematic.modbus.rtu import ModbusException
from diematic.registers.loader import parse_map


def measure(key, address, **extra):
    return {"key": key, "address": address, "type": "uint", "kind": "measure", "name": key, **extra}


def make_map(max_gap=0):
    return parse_map(
        {
            "system": "t",
            "name": "t",
            "max_gap": max_gap,
            "registers": [
                measure("a", 1),
                measure("b", 2),
                measure("c", 5),
                measure("slow_one", 10, poll="slow"),
            ],
        }
    )


class FakeBus:
    def __init__(self, registers):
        self.registers = registers
        self.reads = []
        self.script = []
        self.silent = set()

    async def read_registers(self, start, count, *, max_wait=0):
        self.reads.append((start, count))
        if self.script:
            outcome = self.script.pop(0)
            if outcome is not None:
                raise outcome
        addresses = range(start, start + count)
        if self.silent.intersection(addresses):
            raise SlaveNotAnswering("the slave did not answer")
        if any(a not in self.registers for a in addresses):
            raise ModbusException(rtu.FC_READ_HOLDING, rtu.EXC_ILLEGAL_ADDRESS)
        return [self.registers[a] for a in addresses]

    async def write_registers(self, start, values, *, max_wait=0):
        raise NotImplementedError


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def build(registers=None, regmap=None, clock=None, **options):
    regmap = regmap or make_map()
    bus = FakeBus(registers if registers is not None else {1: 11, 2: 22, 5: 55, 10: 100})
    events = EventBus()
    subscription = events.subscribe()
    options.setdefault("clock", clock or Clock())
    poller = Poller(bus, regmap, StateStore(regmap), events, **options)
    return poller, bus, subscription


def drain(subscription):
    items = []
    while not subscription.queue.empty():
        items.append(subscription.queue.get_nowait())
    return items


def values(events):
    return {e.key: e.value for e in events if isinstance(e, ValueChanged)}


async def test_group_is_read_in_contiguous_blocks_and_published():
    poller, bus, events = build()
    assert await poller.poll_group("fast") is True
    assert bus.reads == [(1, 2), (5, 1)]
    assert values(drain(events)) == {"a": 11, "b": 22, "c": 55}


async def test_only_changes_are_published_on_later_polls():
    poller, bus, events = build()
    await poller.poll_group("fast")
    drain(events)
    await poller.poll_group("fast")
    assert values(drain(events)) == {}
    bus.registers[2] = 23
    await poller.poll_group("fast")
    assert values(drain(events)) == {"b": 23}


async def test_groups_are_polled_separately():
    poller, bus, _ = build()
    await poller.poll_group("slow")
    assert bus.reads == [(10, 1)]


async def test_first_success_announces_the_boiler_online():
    poller, _, events = build()
    await poller.poll_group("fast")
    assert [e for e in drain(events) if isinstance(e, AvailabilityChanged)] == [
        AvailabilityChanged(True)
    ]
    assert poller.online is True


async def test_boiler_goes_offline_after_a_stretch_without_answers_and_recovers():
    clock = Clock()
    poller, bus, events = build(clock=clock, offline_after=120)
    await poller.poll_group("fast")
    drain(events)
    bus.script = [BusTimeout("no window")] * 2
    clock.advance(60)
    assert await poller.poll_group("fast") is False
    assert poller.online is True
    bus.script = [BusTimeout("no window")] * 2
    clock.advance(70)
    await poller.poll_group("fast")
    assert poller.online is False
    assert AvailabilityChanged(False) in drain(events)
    await poller.poll_group("fast")
    assert poller.online is True


async def test_gateway_reporting_a_lost_boiler_counts_as_link_failure():
    clock = Clock()
    poller, bus, _ = build(clock=clock, offline_after=120)
    lost = ModbusException(rtu.FC_READ_HOLDING, rtu.EXC_TARGET_NO_RESPONSE)
    bus.script = [lost, lost]
    await poller.poll_group("fast")
    assert poller.online is None
    clock.advance(130)
    bus.script = [lost, lost]
    await poller.poll_group("fast")
    assert poller.online is False


async def test_write_pending_exception_does_not_affect_availability():
    clock = Clock()
    poller, bus, _ = build(clock=clock, offline_after=1)
    await poller.poll_group("fast")
    clock.advance(500)
    bus.script = [ModbusException(rtu.FC_READ_HOLDING, rtu.EXC_WRITE_PENDING)] * 2
    assert await poller.poll_group("fast") is True
    assert poller.online is True


async def test_rejected_gap_block_falls_back_to_gapless_reads_for_good():
    regmap = make_map(max_gap=5)
    poller, bus, events = build(registers={1: 11, 2: 22, 5: 55, 10: 100}, regmap=regmap)
    await poller.poll_group("fast")
    assert bus.reads == [(1, 5), (1, 2), (5, 1)]
    assert values(drain(events)) == {"a": 11, "b": 22, "c": 55}
    bus.reads.clear()
    await poller.poll_group("fast")
    assert bus.reads == [(1, 2), (5, 1)]


async def test_rejected_register_is_skipped_afterwards():
    poller, bus, events = build(registers={1: 11, 2: 22, 10: 100})
    await poller.poll_group("fast")
    assert values(drain(events)) == {"a": 11, "b": 22}
    bus.reads.clear()
    await poller.poll_group("fast")
    assert bus.reads == [(1, 2)]


async def test_contiguous_block_with_an_unsupported_member_is_split_per_register():
    poller, bus, events = build(registers={1: 11, 10: 100})
    await poller.poll_group("fast")
    assert bus.reads == [(1, 2), (1, 1), (2, 1), (5, 1)]
    assert values(drain(events)) == {"a": 11}
    assert poller.online is True


async def test_run_polls_each_group_on_its_own_interval():
    poller, bus, _ = build(intervals=PollIntervals(fast=0.05, slow=10, retry=0.05))
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.4)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    fast_cycles = bus.reads.count((1, 2))
    assert fast_cycles >= 3
    assert bus.reads.count((10, 1)) == 1


async def test_run_retries_sooner_after_an_unhealthy_cycle():
    poller, bus, _ = build(intervals=PollIntervals(fast=30, slow=30, retry=0.05))
    bus.script = [BusTimeout("x")] * 3
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.4)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert poller.online is True


@pytest.mark.parametrize("group", ["fast", "slow"])
async def test_empty_group_is_a_no_op(group):
    regmap = parse_map({"system": "t", "name": "t", "registers": []})
    poller = Poller(FakeBus({}), regmap, StateStore(regmap), EventBus())
    assert await poller.poll_group(group) is True


async def test_availability_is_mirrored_into_the_state_store():
    regmap = make_map()
    state = StateStore(regmap)
    poller = Poller(FakeBus({1: 1, 2: 2, 5: 5}), regmap, state, EventBus())
    assert state.online is None
    await poller.poll_group("fast")
    assert state.online is True


async def test_registers_the_boiler_never_answers_are_found_and_skipped():
    poller, bus, events = build()
    bus.silent = {2}
    await poller.poll_group("fast")
    assert values(drain(events)) == {"a": 11, "c": 55}
    assert poller.skipped == []
    bus.reads.clear()
    await poller.poll_group("fast")
    assert poller.skipped == ["b"]
    bus.reads.clear()
    assert await poller.poll_group("fast") is True
    assert bus.reads == [(1, 1), (5, 1)]
    assert poller.online is True


async def test_a_mute_register_never_makes_the_boiler_flap():
    clock = Clock()
    poller, bus, events = build(clock=clock, offline_after=120)
    bus.silent = {2}
    states = []
    for _ in range(20):
        await poller.poll_group("fast")
        clock.advance(30)
        states.append(poller.online)
    assert states == [True] * 20
    assert [e for e in drain(events) if isinstance(e, AvailabilityChanged)] == [
        AvailabilityChanged(True)
    ]


async def test_one_missed_answer_does_not_condemn_a_register():
    poller, bus, _ = build()
    bus.silent = {2}
    await poller.poll_group("fast")
    bus.silent = set()
    await poller.poll_group("fast")
    bus.silent = {2}
    await poller.poll_group("fast")
    assert poller.skipped == []


async def test_nothing_is_skipped_while_the_whole_link_is_down():
    poller, bus, _ = build()
    bus.silent = {1, 2, 5}
    for _ in range(3):
        await poller.poll_group("fast")
    assert poller.skipped == []
    assert set(bus.reads) == {(1, 2), (5, 1)}


async def test_skipped_registers_are_tried_again_after_an_hour():
    clock = Clock()
    poller, bus, events = build(clock=clock)
    bus.silent = {2}
    await poller.poll_group("fast")
    await poller.poll_group("fast")
    assert poller.skipped == ["b"]
    bus.silent = set()
    clock.advance(3500)
    await poller.poll_group("fast")
    assert poller.skipped == ["b"]
    clock.advance(200)
    drain(events)
    await poller.poll_group("fast")
    assert poller.skipped == []
    assert values(drain(events)).get("b") == 22
