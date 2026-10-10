import asyncio
import contextlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from diematic.core.events import (
    AvailabilityChanged,
    EventBus,
    SetRequest,
    SetResult,
    ValueChanged,
)
from diematic.core.state import StateStore
from diematic.plugins.base import PluginContext, PluginError
from diematic.plugins.mqtt.plugin import (
    MqttPlugin,
    climate_state,
    format_value,
    parse_flag,
)
from diematic.registers.loader import load_map

REGMAP = load_map("diematic_isystem", "en")
P = "diematic"


class FakeClient:
    def __init__(self):
        self.published = []
        self.subscribed = []
        self._incoming = asyncio.Queue()

    async def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, retain))

    async def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    @property
    def messages(self):
        return self._iterate()

    async def _iterate(self):
        while True:
            item = await self._incoming.get()
            if isinstance(item, Exception):
                raise item
            yield item

    def deliver(self, topic, payload):
        self._incoming.put_nowait(SimpleNamespace(topic=topic, payload=payload))

    def disconnect(self, error):
        self._incoming.put_nowait(error)

    def last(self, topic):
        values = [payload for t, payload, _ in self.published if t == topic]
        return values[-1] if values else None

    def topics(self):
        return [t for t, _, _ in self.published]


class Rig:
    def __init__(self, regmap=REGMAP, **config):
        self.client = FakeClient()
        self.events = EventBus()
        self.state = StateStore(regmap)
        context = PluginContext(regmap, self.state, self.events, "en", config)
        self.plugin = MqttPlugin(context, self._factory)
        self.requests = self.events.subscribe(SetRequest)
        self.task = None

    @asynccontextmanager
    async def _factory(self, settings, topics):
        yield self.client

    async def start(self):
        self.task = asyncio.create_task(self.plugin.run())
        await self.until(lambda: self.client.last(f"{P}/boiler_status") is not None)
        return self

    async def stop(self):
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, ConnectionError):
            await self.task

    async def until(self, condition, limit=2.0):
        deadline = asyncio.get_running_loop().time() + limit
        while not condition():
            if asyncio.get_running_loop().time() > deadline:
                pytest.fail("condition not reached in time")
            await asyncio.sleep(0.01)

    async def request(self):
        return await asyncio.wait_for(self.requests.get(), 1)

    async def settle(self):
        await asyncio.sleep(0.05)


@pytest.fixture
async def rig():
    started = await Rig().start()
    yield started
    await started.stop()


# --- helpers ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "text"),
    [(None, ""), (True, "ON"), (False, "OFF"), (21.5, "21.5"), (22.0, "22"), (0.0, "0"), (7, "7")],
)
def test_format_value(value, text):
    assert format_value(value) == text


@pytest.mark.parametrize(
    ("text", "state"),
    [("ON", True), (" off ", False), ("1", True), ("False", False), ("maybe", None), ("", None)],
)
def test_parse_flag(text, state):
    assert parse_flag(text) is state


@pytest.mark.parametrize(
    ("active", "mode", "preset"),
    [
        ({"auto"}, "auto", "none"),
        ({"frost_protection"}, "off", "none"),
        ({"day"}, "heat", "day"),
        ({"night"}, "heat", "night"),
        ({"auto", "day"}, "auto", "day"),
        (set(), None, "none"),
        ({"dhw"}, None, "none"),
    ],
)
def test_thermostat_state_is_derived_from_flags(active, mode, preset):
    climate = REGMAP.composites[0]
    flags = {name: name in active for name in REGMAP.get(climate.mode_register).bits}
    assert climate_state(climate, flags) == (mode, preset)


# --- connection --------------------------------------------------------------------------


async def test_connecting_subscribes_and_announces_everything(rig):
    client = rig.client
    assert set(client.subscribed) == {
        f"{P}/+/set",
        f"{P}/+/+/set",
        f"{P}/climate/+/+/set",
        "homeassistant/status",
    }
    assert client.last(f"{P}/status") == "online"
    discovery = [
        t for t in client.topics() if t.startswith("homeassistant/") and t.endswith("config")
    ]
    assert len(discovery) == len(rig.plugin._discovery) > 100
    assert json.loads(client.last("homeassistant/sensor/diematic/outdoor_temp/config"))[
        "state_topic"
    ]
    assert client.last(f"{P}/boiler_status") == "offline"


async def test_discovery_messages_are_retained(rig):
    config = [r for r in rig.client.published if r[0].endswith("/config")]
    assert config and all(retained for _, _, retained in config)


async def test_current_state_is_published_when_connecting_late():
    rig = Rig()
    for event in (
        ValueChanged("outdoor_temp", 3.5, (35,)),
        ValueChanged("circuit_a_forcing", {"auto": True, "day": False}, (8,)),
    ):
        rig.state._entries[event.key] = SimpleNamespace(value=event.value, raw=event.raw, updated=0)
    rig.state.online = True
    await rig.start()
    try:
        assert rig.client.last(f"{P}/outdoor_temp") == "3.5"
        assert rig.client.last(f"{P}/circuit_a_forcing/auto") == "ON"
        assert rig.client.last(f"{P}/boiler_status") == "online"
        assert rig.client.last(f"{P}/climate/circuit_a/mode") == "auto"
    finally:
        await rig.stop()


async def test_discovery_can_be_switched_off():
    rig = await Rig(discovery=False).start()
    try:
        assert not any(
            t.startswith("homeassistant/") and t.endswith("config") for t in rig.client.topics()
        )
    finally:
        await rig.stop()


async def test_home_assistant_restart_triggers_a_new_announcement(rig):
    before = len(rig.client.published)
    rig.client.deliver("homeassistant/status", b"offline")
    await rig.settle()
    assert len(rig.client.published) == before
    rig.client.deliver("homeassistant/status", b"online")
    await rig.until(lambda: len(rig.client.published) > before + 100)


async def test_going_offline_is_announced_on_shutdown(rig):
    await rig.stop()
    assert rig.client.last(f"{P}/status") == "offline"


async def test_a_lost_connection_ends_the_session_so_the_supervisor_can_retry(rig):
    rig.client.disconnect(ConnectionError("broker went away"))
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(rig.task, 1)


# --- outgoing ----------------------------------------------------------------------------


async def test_values_are_published_retained_as_text(rig):
    rig.events.publish(ValueChanged("circuit_a_day_temp", 21.5, (215,)))
    rig.events.publish(ValueChanged("circuit_a_room_temp", None, (0xFFFF,)))
    await rig.until(lambda: rig.client.last(f"{P}/circuit_a_room_temp") is not None)
    assert rig.client.last(f"{P}/circuit_a_day_temp") == "21.5"
    assert rig.client.last(f"{P}/circuit_a_room_temp") == ""
    retained = {t: r for t, _, r in rig.client.published if t == f"{P}/circuit_a_day_temp"}
    assert retained[f"{P}/circuit_a_day_temp"] is True


async def test_flag_registers_publish_one_topic_per_flag(rig):
    flags = {name: name == "auto" for name in REGMAP.get("circuit_a_forcing").bits}
    rig.events.publish(ValueChanged("circuit_a_forcing", flags, (8,)))
    await rig.until(lambda: rig.client.last(f"{P}/circuit_a_forcing/auto") is not None)
    assert rig.client.last(f"{P}/circuit_a_forcing/auto") == "ON"
    assert rig.client.last(f"{P}/circuit_a_forcing/night") == "OFF"
    assert f"{P}/circuit_a_forcing" not in rig.client.topics()


async def test_thermostat_mode_follows_its_flag_register(rig):
    flags = {name: name == "night" for name in REGMAP.get("circuit_a_forcing").bits}
    rig.events.publish(ValueChanged("circuit_a_forcing", flags, (2,)))
    await rig.until(lambda: rig.client.last(f"{P}/climate/circuit_a/preset") is not None)
    assert rig.client.last(f"{P}/climate/circuit_a/mode") == "heat"
    assert rig.client.last(f"{P}/climate/circuit_a/preset") == "night"


async def test_boiler_availability_is_published(rig):
    rig.events.publish(AvailabilityChanged(True))
    await rig.until(lambda: rig.client.last(f"{P}/boiler_status") == "online")
    rig.events.publish(AvailabilityChanged(False))
    await rig.until(lambda: rig.client.last(f"{P}/boiler_status") == "offline")


async def test_set_results_are_published_as_json(rig):
    rig.events.publish(SetResult("circuit_a_day_temp", False, "outside the range"))
    await rig.until(lambda: rig.client.last(f"{P}/set_result") is not None)
    assert json.loads(rig.client.last(f"{P}/set_result")) == {
        "key": "circuit_a_day_temp",
        "ok": False,
        "message": "outside the range",
    }


# --- incoming ----------------------------------------------------------------------------


async def test_number_command_becomes_a_numeric_request(rig):
    rig.client.deliver(f"{P}/circuit_a_day_temp/set", b"21.5")
    assert await rig.request() == SetRequest("circuit_a_day_temp", 21.5)


async def test_decimal_comma_is_accepted(rig):
    rig.client.deliver(f"{P}/circuit_a_day_temp/set", "21,5")
    assert await rig.request() == SetRequest("circuit_a_day_temp", 21.5)


async def test_enum_and_text_commands_are_passed_through(rig):
    rig.client.deliver(f"{P}/dhw_priority/set", b"Sliding")
    assert await rig.request() == SetRequest("dhw_priority", "Sliding")
    rig.client.deliver(f"{P}/circuit_a_program_monday/set", b"07:00-20:00")
    assert await rig.request() == SetRequest("circuit_a_program_monday", "07:00-20:00")


async def test_flag_command_becomes_a_bit_request(rig):
    rig.client.deliver(f"{P}/circuit_a_forcing/auto/set", b"ON")
    assert await rig.request() == SetRequest("circuit_a_forcing", bits={"auto": True})
    rig.client.deliver(f"{P}/circuit_a_forcing/auto/set", b"OFF")
    assert await rig.request() == SetRequest("circuit_a_forcing", bits={"auto": False})


async def test_flags_can_be_set_together_with_a_json_object(rig):
    rig.client.deliver(f"{P}/circuit_a_forcing/set", b'{"night": true, "auto": false}')
    assert await rig.request() == SetRequest(
        "circuit_a_forcing", bits={"night": True, "auto": False}
    )


async def test_thermostat_commands_set_one_mode_flag_and_clear_the_others(rig):
    rig.client.deliver(f"{P}/climate/circuit_a/mode/set", b"heat")
    request = await rig.request()
    assert request.key == "circuit_a_forcing"
    assert request.bits == {
        "frost_protection": False,
        "night": False,
        "day": True,
        "auto": False,
    }
    rig.client.deliver(f"{P}/climate/circuit_a/preset/set", b"night")
    assert (await rig.request()).bits["night"] is True


@pytest.mark.parametrize(
    ("topic", "payload"),
    [
        (f"{P}/circuit_a_day_temp/set", b"warm"),
        (f"{P}/circuit_a_day_temp/set", b"nan"),
        (f"{P}/circuit_a_day_temp/set", b"inf"),
        (f"{P}/circuit_a_day_temp/set", b"\xff\xfe"),
        (f"{P}/circuit_a_day_temp/set", b"1" * 2000),
        (f"{P}/no_such_register/set", b"1"),
        (f"{P}/circuit_a_forcing/auto/set", b"perhaps"),
        (f"{P}/circuit_a_forcing/set", b"not json"),
        (f"{P}/circuit_a_forcing/set", b'{"auto": 1}'),
        (f"{P}/climate/circuit_a/mode/set", b"turbo"),
        (f"{P}/climate/circuit_z/mode/set", b"auto"),
        (f"{P}/climate/circuit_a/preset/set", b"sunbathing"),
        ("other/circuit_a_day_temp/set", b"20"),
    ],
)
async def test_invalid_or_foreign_commands_are_dropped(rig, topic, payload):
    rig.client.deliver(topic, payload)
    await rig.settle()
    assert rig.requests.queue.empty()
    assert not rig.task.done()


async def test_a_bad_message_does_not_stop_later_ones(rig):
    rig.client.deliver(f"{P}/circuit_a_day_temp/set", b"warm")
    rig.client.deliver(f"{P}/circuit_a_day_temp/set", b"20")
    assert await rig.request() == SetRequest("circuit_a_day_temp", 20.0)


# --- configuration -----------------------------------------------------------------------


def test_invalid_configuration_is_a_plugin_error_that_hides_secrets():
    context = PluginContext(
        REGMAP, StateStore(REGMAP), EventBus(), "en", {"port": 0, "password": "s3cret"}
    )
    with pytest.raises(PluginError) as info:
        MqttPlugin(context)
    assert "port" in str(info.value) and "s3cret" not in str(info.value)


def test_unknown_configuration_keys_are_rejected():
    context = PluginContext(REGMAP, StateStore(REGMAP), EventBus(), "en", {"hots": "x"})
    with pytest.raises(PluginError, match="hots"):
        MqttPlugin(context)


async def test_the_password_never_reaches_the_logs(caplog):
    caplog.set_level("DEBUG")
    rig = Rig(username="bridge", password="s3cret-password-value")
    await rig.start()
    try:
        rig.client.deliver(f"{P}/circuit_a_day_temp/set", b"warm")
        rig.client.deliver(f"{P}/climate/circuit_z/mode/set", b"auto")
        rig.events.publish(SetResult("circuit_a_day_temp", False, "refused"))
        await rig.settle()
    finally:
        await rig.stop()
    assert "s3cret-password-value" not in caplog.text
    assert "s3cret-password-value" not in repr(rig.plugin.settings)
    assert all("s3cret-password-value" not in str(p) for p in rig.client.published)


async def test_entities_of_left_out_registers_are_retired_on_connect():
    rig = await Rig(REGMAP.excluding(["circuit_c", "solar"])).start()
    try:
        published = rig.client.published
        retired = {t: (payload, retained) for t, payload, retained in published if payload is None}
        assert retired["homeassistant/climate/diematic/circuit_c/config"] == (None, True)
        assert retired["diematic/circuit_c_day_temp"] == (None, True)
        announced = {t for t, payload, _ in published if payload is not None}
        assert "homeassistant/climate/diematic/circuit_c/config" not in announced
        assert "homeassistant/climate/diematic/circuit_a/config" in announced
    finally:
        await rig.stop()


async def test_nothing_is_retired_when_every_register_is_kept(rig):
    assert not [t for t, payload, _ in rig.client.published if payload is None]


async def test_a_silent_boiler_does_not_make_the_entities_unavailable(rig):
    rig.events.publish(AvailabilityChanged(True))
    await rig.until(lambda: rig.client.last(f"{P}/boiler_status") == "online")
    rig.events.publish(ValueChanged("circuit_a_day_temp", 21.5, (215,)))
    await rig.until(lambda: rig.client.last(f"{P}/circuit_a_day_temp") == "21.5")
    rig.events.publish(AvailabilityChanged(False))
    await rig.until(lambda: rig.client.last(f"{P}/boiler_status") == "offline")
    assert rig.client.last(f"{P}/circuit_a_day_temp") == "21.5"
    assert rig.client.last(f"{P}/status") == "online"
    assert not [t for t, payload, _ in rig.client.published if payload is None]


async def test_the_boiler_time_is_published_like_any_other_value(rig):
    rig.events.publish(ValueChanged("boiler_time", "2026-07-15T10:30:00+02:00", ()))
    await rig.until(lambda: rig.client.last(f"{P}/boiler_time") is not None)
    assert rig.client.last(f"{P}/boiler_time") == "2026-07-15T10:30:00+02:00"
    assert rig.client.last("homeassistant/sensor/diematic/boiler_time/config")


async def test_the_boiler_time_known_at_connect_time_is_published_with_the_snapshot():
    rig = Rig()
    for key, word in [
        ("clock_year", 26),
        ("clock_month", 7),
        ("clock_day", 15),
        ("clock_hour", 10),
        ("clock_minute", 30),
    ]:
        rig.state.ingest(REGMAP.get(key), [word])
    await rig.start()
    try:
        assert rig.client.last(f"{P}/boiler_time") == "2026-07-15T10:30:00+00:00"
    finally:
        await rig.stop()
