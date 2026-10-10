import asyncio
from importlib.metadata import EntryPoint
from typing import ClassVar

import pytest

from diematic.app import build_runtime, run_application
from diematic.config.settings import AppSettings, BusSettings, GatewaySettings
from diematic.core.events import AvailabilityChanged, SetRequest, SetResult, ValueChanged
from diematic.plugins.base import Plugin, PluginError
from diematic.registers.loader import MapError, load_map
from diematic.sim.boiler import BoilerSimulator
from diematic.sim.defaults import default_registers

MAP = """
system: tiny
name: Tiny test system
slave_address: 10
registers:
  - key: outdoor_temp
    address: 601
    type: temp
    kind: measure
    name: Outdoor temperature
  - key: setpoint
    address: 650
    type: temp
    kind: parameter
    name: Setpoint
    min: 5
    max: 30
    step: 0.5
    poll: fast
"""

FAST_BUS = BusSettings(
    silence_gap=0.1, master_phase=0.25, cycle=0.9, cycle_guard=0.05, window=0.5, idle_open_after=0.7
)

SEEN = []


class RecordingPlugin(Plugin):
    """Collects every event so tests can observe what a real plugin would see."""

    instances: ClassVar[list] = []

    async def run(self) -> None:
        RecordingPlugin.instances.append(self)
        subscription = self.context.events.subscribe()
        while True:
            SEEN.append(await subscription.get())


class CrashingPlugin(Plugin):
    starts = 0

    async def run(self) -> None:
        CrashingPlugin.starts += 1
        raise RuntimeError("plugin failure")


def discover(**classes):
    return lambda: [
        EntryPoint(name, f"{__name__}:{cls.__name__}", "diematic.plugins")
        for name, cls in classes.items()
    ]


@pytest.fixture(autouse=True)
def reset_recorders():
    SEEN.clear()
    RecordingPlugin.instances.clear()
    CrashingPlugin.starts = 0


@pytest.fixture
def map_file(tmp_path):
    path = tmp_path / "tiny.yaml"
    path.write_text(MAP, encoding="utf-8")
    return path


@pytest.fixture
async def simulator(map_file):
    regmap = load_map(map_file)
    sim = BoilerSimulator(
        registers=default_registers(regmap),
        master_phase=0.3,
        silent_phase=0.6,
        master_frame_interval=0.05,
    )
    yield sim, await sim.start()
    await sim.stop()


def settings_for(port, map_file, plugins=("rec",), **gateway):
    values = {
        "host": "127.0.0.1",
        "port": port,
        "map": str(map_file),
        "bus": FAST_BUS,
        "language": "en",
        "fast_interval": 1,
        **gateway,
    }
    return AppSettings(GatewaySettings(**values), list(plugins))


class Running:
    def __init__(self, settings, discover_):
        self.stop = asyncio.Event()
        self.task = asyncio.create_task(run_application(settings, self.stop, discover_))

    async def shutdown(self):
        self.stop.set()
        await asyncio.wait_for(self.task, 5)


async def wait_until(condition, limit=8.0):
    deadline = asyncio.get_running_loop().time() + limit
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("condition not reached in time")
        await asyncio.sleep(0.05)


async def test_values_and_availability_reach_plugins(simulator, map_file):
    _, port = simulator
    app = Running(settings_for(port, map_file), discover(rec=RecordingPlugin))
    try:
        await wait_until(
            lambda: any(isinstance(e, ValueChanged) and e.key == "setpoint" for e in SEEN)
        )
        assert AvailabilityChanged(True) in SEEN
        setpoint = next(e for e in SEEN if isinstance(e, ValueChanged) and e.key == "setpoint")
        assert setpoint.value == 17.5
    finally:
        await app.shutdown()


async def test_set_request_reaches_the_boiler_and_is_confirmed(simulator, map_file):
    sim, port = simulator
    app = Running(settings_for(port, map_file), discover(rec=RecordingPlugin))
    try:
        await wait_until(lambda: RecordingPlugin.instances)
        RecordingPlugin.instances[0].context.events.publish(SetRequest("setpoint", 22.0))
        await wait_until(lambda: any(isinstance(e, SetResult) for e in SEEN))
        assert SetResult("setpoint", True) in SEEN
        assert sim.registers[650] == 220
        assert ValueChanged("setpoint", 22.0, (220,)) in SEEN
        assert sim.collisions == 0
    finally:
        await app.shutdown()


async def test_read_only_mode_refuses_changes(simulator, map_file):
    sim, port = simulator
    app = Running(settings_for(port, map_file, read_only=True), discover(rec=RecordingPlugin))
    try:
        await wait_until(lambda: RecordingPlugin.instances)
        RecordingPlugin.instances[0].context.events.publish(SetRequest("setpoint", 22.0))
        await wait_until(lambda: any(isinstance(e, SetResult) for e in SEEN))
        result = next(e for e in SEEN if isinstance(e, SetResult))
        assert not result.ok and sim.writes == []
    finally:
        await app.shutdown()


async def test_a_crashing_plugin_does_not_disturb_the_rest(simulator, map_file):
    _, port = simulator
    app = Running(
        settings_for(port, map_file, plugins=("rec", "bad")),
        discover(rec=RecordingPlugin, bad=CrashingPlugin),
    )
    try:
        await wait_until(lambda: CrashingPlugin.starts >= 2)
        await wait_until(lambda: any(isinstance(e, ValueChanged) for e in SEEN))
        assert not app.task.done()
    finally:
        await app.shutdown()


async def test_the_application_survives_an_unreachable_gateway(map_file):
    app = Running(settings_for(1, map_file), discover(rec=RecordingPlugin))
    await asyncio.sleep(0.5)
    assert not app.task.done()
    await app.shutdown()


async def test_stop_returns_promptly(simulator, map_file):
    _, port = simulator
    app = Running(settings_for(port, map_file), discover(rec=RecordingPlugin))
    await asyncio.sleep(0.2)
    await app.shutdown()


def test_unknown_map_is_a_map_error(map_file):
    with pytest.raises(MapError):
        build_runtime(settings_for(1, map_file, map="nope"), discover())


def test_unknown_plugin_is_a_plugin_error(map_file):
    with pytest.raises(PluginError):
        build_runtime(settings_for(1, map_file, plugins=("ghost",)), discover())


def test_slave_address_defaults_to_the_one_from_the_map_and_can_be_overridden(map_file):
    default = build_runtime(settings_for(1, map_file, plugins=()), discover())
    assert default.bus._slave == 10
    custom = build_runtime(settings_for(1, map_file, plugins=(), slave_address=12), discover())
    assert custom.bus._slave == 12


def test_clock_sync_is_only_built_when_enabled_and_supported(map_file):
    assert build_runtime(settings_for(1, map_file, plugins=()), discover()).clock_sync is None
    unsupported = settings_for(1, map_file, plugins=(), clock_sync=True)
    assert build_runtime(unsupported, discover()).clock_sync is None
    supported = settings_for(1, map_file, plugins=(), clock_sync=True, map="diematic_isystem")
    assert build_runtime(supported, discover()).clock_sync is not None


def test_disabled_registers_are_left_out_of_the_runtime(map_file):
    settings = settings_for(1, map_file, plugins=(), disabled_registers=["outdoor_temp"])
    runtime = build_runtime(settings, discover())
    assert "outdoor_temp" not in runtime.regmap and "setpoint" in runtime.regmap
    assert runtime.regmap.unfiltered is not None


def test_disabling_an_unknown_register_stops_the_start_with_a_clear_error(map_file):
    settings = settings_for(1, map_file, plugins=(), disabled_registers=["no_such_thing"])
    with pytest.raises(MapError, match="no_such_thing"):
        build_runtime(settings, discover())


def test_the_retry_interval_never_exceeds_the_regular_one(map_file):
    quick = build_runtime(settings_for(1, map_file, plugins=(), fast_interval=3), discover())
    assert quick.poller._intervals.retry == 3
    default = build_runtime(settings_for(1, map_file, plugins=(), fast_interval=30), discover())
    assert default.poller._intervals.retry == 10
