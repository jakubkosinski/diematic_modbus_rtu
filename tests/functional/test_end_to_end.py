"""The whole stack: simulated boiler, application, MQTT broker and a Home Assistant stand-in."""

import asyncio
import contextlib
import json
import socket

import aiomqtt
import pytest
from amqtt.broker import Broker

from diematic.app import run_application
from diematic.config.settings import AppSettings, BusSettings, GatewaySettings
from diematic.registers.codecs import TimeProgramCodec
from diematic.registers.loader import load_map
from diematic.sim.boiler import BoilerSimulator
from diematic.sim.defaults import default_registers

MAP = """
system: tiny
name: Tiny boiler
registers:
  - key: room_temp
    address: 20
    type: temp
    kind: measure
    name: Room temperature
    unit: "°C"
    device_class: temperature
  - key: night_temp
    address: 21
    type: temp
    kind: parameter
    name: Night temperature
    min: 5
    max: 30
    step: 0.5
    poll: fast
  - key: day_temp
    address: 22
    type: temp
    kind: parameter
    name: Day temperature
    min: 5
    max: 30
    step: 0.5
    poll: fast
  - key: mode
    address: 23
    type: bits
    kind: parameter
    name: Mode
    poll: fast
    bits:
      frost: {bit: 0, name: Frost, writable: true}
      night: {bit: 1, name: Night, writable: true}
      day: {bit: 2, name: Day, writable: true}
      auto: {bit: 3, name: Auto, writable: true}
      dhw: {bit: 4, name: DHW, writable: true}
  - key: ghost
    address: 24
    type: uint
    kind: measure
    name: Register this boiler does not have
  - key: clock_year
    address: 100
    type: uint
    kind: parameter
    name: Clock year
    poll: fast
  - key: clock_month
    address: 101
    type: uint
    kind: parameter
    name: Clock month
    poll: fast
  - key: clock_day
    address: 102
    type: uint
    kind: parameter
    name: Clock day
    poll: fast
  - key: clock_hour
    address: 103
    type: uint
    kind: parameter
    name: Clock hour
    poll: fast
  - key: clock_minute
    address: 104
    type: uint
    kind: parameter
    name: Clock minute
    poll: fast
time_programs:
  - {key: program, name: Program, address: 30}
composites:
  - key: circuit_a
    name: Circuit A
    current_temperature: room_temp
    low: night_temp
    high: day_temp
    mode_register: mode
    modes: {auto: auto, heat: day, "off": frost}
    presets: {night: night, day: day}
    exclusive: [frost, night, day, auto]
timestamps:
  - key: boiler_time
    name: Boiler time
    year: clock_year
    month: clock_month
    day: clock_day
    hour: clock_hour
    minute: clock_minute
"""

FAST_BUS = BusSettings(
    silence_gap=0.1,
    master_phase=0.25,
    cycle=0.9,
    cycle_guard=0.05,
    window=0.5,
    idle_open_after=0.7,
    response_timeout=0.3,
)


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class BrokerHandle:
    def __init__(self, port):
        self.port = port
        self.broker = None

    async def start(self):
        config = {
            "listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{self.port}"}},
            "sys_interval": 0,
            "auth": {"allow-anonymous": True},
            "topic-check": {"enabled": False},
        }
        self.broker = Broker(config)
        await self.broker.start()

    async def stop(self):
        await self.broker.shutdown()


class HomeAssistant:
    """Subscribes to everything the bridge publishes and can send commands."""

    def __init__(self, port):
        self.port = port
        self.seen: dict[str, str] = {}
        self.history: dict[str, list[str]] = {}
        self.client = None
        self._task = None
        self._ready = asyncio.Event()

    async def start(self):
        self._task = asyncio.create_task(self._listen())
        await asyncio.wait_for(self._ready.wait(), 5)

    async def _listen(self):
        async with aiomqtt.Client("127.0.0.1", self.port) as client:
            self.client = client
            await client.subscribe("diematic/#", qos=1)
            await client.subscribe("homeassistant/#", qos=1)
            self._ready.set()
            async for message in client.messages:
                payload = bytes(message.payload).decode()
                self.seen[str(message.topic)] = payload
                self.history.setdefault(str(message.topic), []).append(payload)

    async def stop(self):
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, aiomqtt.MqttError):
            await self._task

    async def command(self, topic, payload):
        await self.client.publish(topic, payload, qos=1)

    async def wait(self, topic, expected=None, limit=10.0):
        deadline = asyncio.get_running_loop().time() + limit
        while True:
            value = self.seen.get(topic)
            if value is not None and (expected is None or value == expected):
                return value
            if asyncio.get_running_loop().time() > deadline:
                pytest.fail(f"{topic} never became {expected!r}; last value {value!r}")
            await asyncio.sleep(0.05)


@pytest.fixture
async def stack(tmp_path):
    map_file = tmp_path / "tiny.yaml"
    map_file.write_text(MAP, encoding="utf-8")
    regmap = load_map(map_file)
    broker = BrokerHandle(free_port())
    await broker.start()
    registers = default_registers(regmap)
    registers[20] = 205
    registers.update({100: 26, 101: 7, 102: 15, 103: 10, 104: 30})
    simulator = BoilerSimulator(
        registers=registers, master_phase=0.3, silent_phase=0.6, master_frame_interval=0.05
    )
    gateway_port = await simulator.start()
    settings = AppSettings(
        GatewaySettings(
            host="127.0.0.1",
            port=gateway_port,
            map=str(map_file),
            bus=FAST_BUS,
            fast_interval=1,
            language="en",
            timezone="Europe/Warsaw",
        ),
        ["mqtt"],
        {"mqtt": {"host": "127.0.0.1", "port": broker.port, "client_id": "bridge"}},
    )
    stop = asyncio.Event()
    app = asyncio.create_task(run_application(settings, stop))
    home = HomeAssistant(broker.port)
    await home.start()
    stack = SimpleStack(broker, simulator, home, app, stop)
    yield stack
    stop.set()
    await asyncio.wait_for(app, 10)
    await stack.home.stop()
    await simulator.stop()
    with contextlib.suppress(Exception):
        await broker.stop()


class SimpleStack:
    def __init__(self, broker, simulator, home, app, stop):
        self.broker, self.simulator, self.home, self.app, self.stop = (
            broker,
            simulator,
            home,
            app,
            stop,
        )


async def test_values_and_discovery_reach_home_assistant(stack):
    home = stack.home
    assert await home.wait("diematic/room_temp", "20.5") == "20.5"
    assert await home.wait("diematic/boiler_status", "online") == "online"
    assert await home.wait("diematic/status", "online") == "online"
    sensor = json.loads(await home.wait("homeassistant/sensor/diematic/room_temp/config"))
    assert sensor["unit_of_measurement"] == "°C" and sensor["state_topic"] == "diematic/room_temp"
    climate = json.loads(await home.wait("homeassistant/climate/diematic/circuit_a/config"))
    assert climate["mode_command_topic"] == "diematic/climate/circuit_a/mode/set"
    assert "homeassistant/text/diematic/program_monday/config" in home.seen
    assert await home.wait("diematic/program_monday", "07:00-20:00")


async def test_changing_a_setpoint_from_home_assistant_reaches_the_boiler(stack):
    home = stack.home
    await home.wait("diematic/day_temp", "17.5")
    await home.command("diematic/day_temp/set", "22")
    await home.wait("diematic/day_temp", "22")
    assert stack.simulator.registers[22] == 220
    result = json.loads(await home.wait("diematic/set_result"))
    assert result == {"key": "day_temp", "ok": True, "message": ""}
    assert stack.simulator.collisions == 0


async def test_out_of_range_setpoint_is_refused_and_reported(stack):
    home = stack.home
    await home.wait("diematic/day_temp", "17.5")
    await home.command("diematic/day_temp/set", "99")
    result = json.loads(await home.wait("diematic/set_result"))
    assert result["ok"] is False and "range" in result["message"]
    assert stack.simulator.registers[22] == 175


async def test_thermostat_mode_roundtrip_preserves_unrelated_flags(stack):
    home = stack.home
    stack.simulator.registers[23] = 0b10000
    await home.wait("diematic/mode/dhw", "ON")
    await home.command("diematic/climate/circuit_a/mode/set", "heat")
    await home.wait("diematic/climate/circuit_a/mode", "heat")
    assert stack.simulator.registers[23] == 0b10100
    await home.command("diematic/climate/circuit_a/preset/set", "night")
    await home.wait("diematic/climate/circuit_a/preset", "night")
    assert stack.simulator.registers[23] == 0b10010


async def test_weekly_program_can_be_edited_as_text(stack):
    home = stack.home
    await home.wait("diematic/program_monday", "07:00-20:00")
    await home.command("diematic/program_monday/set", "06:00-08:00,17:00-22:30")
    await home.wait("diematic/program_monday", "06:00-08:00,17:00-22:30")
    expected = TimeProgramCodec().encode("06:00-08:00,17:00-22:30")
    assert [stack.simulator.registers[30 + i] for i in range(3)] == expected


async def test_bridge_keeps_polling_and_reconnects_when_the_broker_restarts(stack):
    home = stack.home
    await home.wait("diematic/room_temp", "20.5")
    await stack.broker.stop()
    stack.simulator.registers[20] = 231
    await asyncio.sleep(1.5)
    assert not stack.app.done()
    await stack.broker.start()
    await home.stop()
    stack.home = HomeAssistant(stack.broker.port)
    await stack.home.start()
    assert await stack.home.wait("diematic/room_temp", "23.1", limit=25) == "23.1"


async def test_registers_the_boiler_ignores_neither_flap_it_nor_break_the_others(stack, caplog):
    caplog.set_level("INFO")
    home = stack.home
    stack.simulator.silent_addresses = {24}
    await home.wait("diematic/boiler_status", "online")
    await home.wait("diematic/room_temp", "20.5")
    await asyncio.sleep(9)
    statuses = home.history["diematic/boiler_status"]
    assert "offline" not in statuses[statuses.index("online") :]
    assert await home.wait("diematic/day_temp", "17.5") == "17.5"
    assert "diematic/ghost" not in home.seen
    skipped = [r.message for r in caplog.records if "does not answer 1 registers" in r.message]
    assert skipped and "DIEMATIC_DISABLED_REGISTERS=ghost" in skipped[0]
    assert stack.simulator.collisions == 0


async def test_the_boiler_time_is_composed_in_the_configured_zone_and_follows_the_clock(stack):
    home = stack.home
    assert await home.wait("diematic/boiler_time", "2026-07-15T10:30:00+02:00")
    sensor = json.loads(await home.wait("homeassistant/sensor/diematic/boiler_time/config"))
    assert sensor["device_class"] == "timestamp"
    assert sensor["state_topic"] == "diematic/boiler_time"
    stack.simulator.registers[104] = 31
    assert await home.wait("diematic/boiler_time", "2026-07-15T10:31:00+02:00")
    stack.simulator.registers.update({101: 12, 102: 1})
    assert await home.wait("diematic/boiler_time", "2026-12-01T10:31:00+01:00")
