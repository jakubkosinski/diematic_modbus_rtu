from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from diematic.core.clock_sync import CLOCK_KEYS, ClockSync
from diematic.core.state import StateStore
from diematic.modbus.bus import BusTimeout
from diematic.registers.loader import load_map, parse_map

TZ = ZoneInfo("Europe/Warsaw")
HOST_NOW = datetime(2026, 3, 10, 14, 30, tzinfo=TZ)


class FakeBus:
    def __init__(self):
        self.writes = []
        self.error = None

    async def write_registers(self, start, values, *, max_wait=0):
        if self.error:
            raise self.error
        self.writes.append((start, list(values)))

    async def read_registers(self, start, count, *, max_wait=0):
        raise NotImplementedError


def boiler_clock(state, regmap, moment):
    raw = {
        "clock_hour": moment.hour,
        "clock_minute": moment.minute,
        "clock_weekday": moment.isoweekday(),
        "clock_day": moment.day,
        "clock_month": moment.month,
        "clock_year": moment.year % 100,
    }
    for key, value in raw.items():
        state.ingest(regmap.get(key), [value])


@pytest.fixture
def parts():
    regmap = load_map("diematic_isystem")
    state = StateStore(regmap)
    bus = FakeBus()
    sync = ClockSync(bus, regmap, state, TZ, confirmations=3, now=lambda tz: HOST_NOW)
    return sync, bus, state, regmap


def test_supported_requires_all_clock_registers():
    assert ClockSync.supported(load_map("diematic_isystem"))
    assert ClockSync.supported(load_map("diematic3_legacy"))
    assert not ClockSync.supported(parse_map({"system": "t", "name": "t"}))


def test_clock_keys_exist_in_both_shipped_maps():
    for name in ("diematic_isystem", "diematic3_legacy"):
        assert all(key in load_map(name) for key in CLOCK_KEYS)


def test_boiler_time_is_unknown_until_every_register_was_read(parts):
    sync, _, state, regmap = parts
    assert sync.boiler_time() is None and sync.drift() is None
    state.ingest(regmap.get("clock_hour"), [14])
    assert sync.boiler_time() is None


def test_drift_is_the_boiler_clock_minus_the_host_clock(parts):
    sync, _, state, regmap = parts
    boiler_clock(state, regmap, datetime(2026, 3, 10, 14, 33))
    assert sync.drift() == 180


def test_impossible_boiler_date_is_treated_as_unknown(parts):
    sync, _, state, regmap = parts
    boiler_clock(state, regmap, HOST_NOW)
    state.ingest(regmap.get("clock_day"), [31])
    state.ingest(regmap.get("clock_month"), [2])
    assert sync.boiler_time() is None


async def test_small_drift_is_left_alone(parts):
    sync, bus, state, regmap = parts
    boiler_clock(state, regmap, datetime(2026, 3, 10, 14, 30))
    for _ in range(10):
        assert await sync.check() is False
    assert bus.writes == []


async def test_clock_is_set_only_after_the_drift_persisted(parts):
    sync, bus, state, regmap = parts
    boiler_clock(state, regmap, datetime(2026, 3, 10, 14, 40))
    assert [await sync.check() for _ in range(3)] == [False, False, True]
    assert bus.writes == [
        (regmap.get("clock_hour").address, [14]),
        (regmap.get("clock_minute").address, [30]),
        (regmap.get("clock_weekday").address, [2]),
        (regmap.get("clock_day").address, [10]),
        (regmap.get("clock_month").address, [3]),
        (regmap.get("clock_year").address, [26]),
    ]


async def test_a_corrected_drift_resets_the_counter(parts):
    sync, bus, state, regmap = parts
    boiler_clock(state, regmap, datetime(2026, 3, 10, 14, 40))
    await sync.check()
    await sync.check()
    boiler_clock(state, regmap, HOST_NOW)
    await sync.check()
    boiler_clock(state, regmap, datetime(2026, 3, 10, 14, 40))
    assert await sync.check() is False
    assert bus.writes == []


async def test_bus_errors_surface_to_the_caller_of_check(parts):
    sync, bus, state, regmap = parts
    boiler_clock(state, regmap, datetime(2026, 3, 10, 14, 40))
    bus.error = BusTimeout("no window")
    await sync.check()
    await sync.check()
    with pytest.raises(BusTimeout):
        await sync.check()
