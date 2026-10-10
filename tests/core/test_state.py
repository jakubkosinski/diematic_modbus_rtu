from zoneinfo import ZoneInfo

import pytest

from diematic.core.events import ValueChanged
from diematic.core.state import StateStore
from diematic.registers.loader import parse_map

RAW = {
    "system": "t",
    "name": "t",
    "registers": [
        {"key": "temp", "address": 1, "type": "temp", "kind": "measure", "name": "Temp"},
        {"key": "tens", "address": 2, "type": "uint", "kind": "measure", "name": "Tens"},
        {"key": "units", "address": 3, "type": "uint", "kind": "measure", "name": "Units"},
        {"key": "kwh", "address": 4, "type": "uint", "kind": "measure", "name": "kWh"},
        {"key": "wh", "address": 5, "type": "uint", "kind": "measure", "name": "Wh"},
    ],
    "derived": [
        {
            "key": "starts",
            "name": "Starts",
            "terms": [{"key": "tens", "factor": 10}, {"key": "units"}],
        },
        {
            "key": "energy",
            "name": "Energy",
            "terms": [{"key": "kwh"}, {"key": "wh", "factor": 0.001}],
        },
    ],
}


@pytest.fixture
def regmap():
    return parse_map(RAW)


@pytest.fixture
def store(regmap):
    return StateStore(regmap)


def test_first_ingest_reports_a_change_with_decoded_value(store, regmap):
    events = store.ingest(regmap.get("temp"), [0x8005])
    assert events == [ValueChanged("temp", -0.5, (0x8005,))]
    assert store.value("temp") == -0.5 and store.raw("temp") == (0x8005,)


def test_identical_ingest_reports_nothing(store, regmap):
    store.ingest(regmap.get("temp"), [215])
    assert store.ingest(regmap.get("temp"), [215]) == []


def test_changed_ingest_reports_the_new_value(store, regmap):
    store.ingest(regmap.get("temp"), [215])
    assert store.ingest(regmap.get("temp"), [216])[0].value == 21.6


def test_unavailable_marker_is_a_none_value(store, regmap):
    events = store.ingest(regmap.get("temp"), [0xFFFF])
    assert events[0].value is None and store.known("temp")


def test_derived_waits_until_every_term_is_known(store, regmap):
    assert [e.key for e in store.ingest(regmap.get("tens"), [4])] == ["tens"]
    events = store.ingest(regmap.get("units"), [7])
    assert [(e.key, e.value) for e in events] == [("units", 7), ("starts", 47)]


def test_derived_follows_term_changes_and_keeps_integers(store, regmap):
    store.ingest(regmap.get("tens"), [4])
    store.ingest(regmap.get("units"), [7])
    events = store.ingest(regmap.get("tens"), [5])
    assert [(e.key, e.value) for e in events] == [("tens", 5), ("starts", 57)]
    assert isinstance(store.value("starts"), int)


def test_derived_with_fractional_factor_is_rounded(store, regmap):
    store.ingest(regmap.get("kwh"), [100])
    events = store.ingest(regmap.get("wh"), [250])
    assert events[-1].key == "energy" and events[-1].value == 100.25


def test_derived_becomes_none_when_a_term_is_unavailable(store):
    regmap = parse_map(
        {
            **RAW,
            "registers": [
                {
                    "key": "a",
                    "address": 1,
                    "type": "uint",
                    "kind": "measure",
                    "name": "A",
                    "invalid": [0xFFFF],
                },
                {"key": "b", "address": 2, "type": "uint", "kind": "measure", "name": "B"},
            ],
            "derived": [{"key": "sum", "name": "S", "terms": [{"key": "a"}, {"key": "b"}]}],
        }
    )
    store = StateStore(regmap)
    store.ingest(regmap.get("a"), [1])
    store.ingest(regmap.get("b"), [2])
    events = store.ingest(regmap.get("a"), [0xFFFF])
    assert events[-1] == ValueChanged("sum", None, ())


def test_snapshot_lists_everything_known(store, regmap):
    store.ingest(regmap.get("tens"), [1])
    store.ingest(regmap.get("units"), [2])
    assert {e.key: e.value for e in store.snapshot()} == {"tens": 1, "units": 2, "starts": 12}


CLOCK_MAP = {
    "system": "t",
    "name": "t",
    "registers": [
        {"key": "year", "address": 1, "type": "uint", "kind": "parameter", "name": "Y"},
        {
            "key": "month",
            "address": 2,
            "type": "enum",
            "kind": "parameter",
            "name": "M",
            "values": {7: "July", 12: "December"},
        },
        {"key": "day", "address": 3, "type": "uint", "kind": "parameter", "name": "D"},
        {"key": "hour", "address": 4, "type": "uint", "kind": "parameter", "name": "H"},
        {"key": "minute", "address": 5, "type": "uint", "kind": "parameter", "name": "Mi"},
    ],
    "timestamps": [
        {
            "key": "boiler_time",
            "name": "Boiler time",
            "year": "year",
            "month": "month",
            "day": "day",
            "hour": "hour",
            "minute": "minute",
        }
    ],
}


def feed_clock(store, regmap, **words):
    events = []
    for key, word in words.items():
        events += store.ingest(regmap.get(key), [word])
    return events


def stamp_events(events):
    return [e for e in events if e.key == "boiler_time"]


class TestTimestamp:
    def setup_method(self):
        self.regmap = parse_map(CLOCK_MAP)

    def test_nothing_is_published_until_every_part_is_known(self):
        store = StateStore(self.regmap)
        events = feed_clock(store, self.regmap, year=26, month=7, day=15, hour=10)
        assert stamp_events(events) == []

    def test_the_moment_is_composed_in_the_configured_zone(self):
        store = StateStore(self.regmap, ZoneInfo("Europe/Warsaw"))
        events = feed_clock(store, self.regmap, year=26, month=7, day=15, hour=10, minute=30)
        assert stamp_events(events)[-1].value == "2026-07-15T10:30:00+02:00"

    def test_the_offset_follows_daylight_saving_time(self):
        store = StateStore(self.regmap, ZoneInfo("Europe/Warsaw"))
        feed_clock(store, self.regmap, year=26, month=7, day=15, hour=10, minute=30)
        events = feed_clock(store, self.regmap, month=12)
        assert stamp_events(events)[-1].value == "2026-12-15T10:30:00+01:00"

    def test_the_default_zone_is_utc(self):
        store = StateStore(self.regmap)
        feed_clock(store, self.regmap, year=26, month=7, day=15, hour=10, minute=30)
        assert store.value("boiler_time") == "2026-07-15T10:30:00+00:00"

    def test_a_month_given_as_an_enum_label_still_composes(self):
        store = StateStore(self.regmap)
        feed_clock(store, self.regmap, year=26, month=12, day=1, hour=0, minute=0)
        assert store.value("month") == "December"
        assert store.value("boiler_time") == "2026-12-01T00:00:00+00:00"

    def test_it_follows_the_clock_and_stays_quiet_when_nothing_changes(self):
        store = StateStore(self.regmap)
        feed_clock(store, self.regmap, year=26, month=7, day=15, hour=10, minute=30)
        assert stamp_events(feed_clock(store, self.regmap, minute=30)) == []
        assert stamp_events(feed_clock(store, self.regmap, minute=31))[0].value.endswith(
            "10:31:00+00:00"
        )

    def test_an_impossible_date_gives_no_value(self):
        store = StateStore(self.regmap)
        feed_clock(store, self.regmap, year=26, month=7, day=31, hour=10, minute=0)
        events = feed_clock(store, self.regmap, day=32)
        assert stamp_events(events)[-1].value is None

    def test_the_moment_is_part_of_the_snapshot(self):
        store = StateStore(self.regmap)
        feed_clock(store, self.regmap, year=26, month=7, day=15, hour=10, minute=30)
        assert {e.key: e.value for e in store.snapshot()}[
            "boiler_time"
        ] == "2026-07-15T10:30:00+00:00"
