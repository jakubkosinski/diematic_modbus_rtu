"""Assembles the components from the settings and runs them under a supervisor."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from diematic.config.settings import AppSettings
from diematic.core.clock_sync import ClockSync
from diematic.core.events import EventBus
from diematic.core.heartbeat import run_heartbeat
from diematic.core.poller import Poller, PollIntervals
from diematic.core.state import StateStore
from diematic.core.supervisor import Supervisor
from diematic.core.writer import Writer, WriterOptions
from diematic.modbus.bus import DiematicBus
from diematic.plugins.base import Plugin, PluginContext
from diematic.plugins.loader import EntryPoints, create_plugins, installed_plugins
from diematic.registers.loader import RegisterMap, load_map
from diematic.transport.tcp import TcpTransport

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    regmap: RegisterMap
    bus: DiematicBus
    state: StateStore
    events: EventBus
    poller: Poller
    writer: Writer
    plugins: dict[str, Plugin]
    clock_sync: ClockSync | None
    heartbeat_file: Path | None


def build_runtime(settings: AppSettings, discover: EntryPoints = installed_plugins) -> Runtime:
    """Create every component. Raises MapError or PluginError for unusable configuration."""
    gateway = settings.gateway
    regmap = load_map(gateway.map, gateway.language)
    transport = TcpTransport(gateway.host, gateway.port, gateway.connect_timeout)
    bus = DiematicBus(
        transport,
        gateway.slave_address or regmap.slave_address,
        gateway.bus.timing(),
        gateway.trailing_zero,
    )
    events = EventBus()
    state = StateStore(regmap)
    poller = Poller(
        bus, regmap, state, events, PollIntervals(gateway.fast_interval, gateway.slow_interval)
    )
    writer = Writer(bus, regmap, state, events, WriterOptions(read_only=gateway.read_only))

    def context_for(name: str) -> PluginContext:
        config = settings.plugin_config.get(name, {})
        return PluginContext(regmap, state, events, gateway.language, config)

    plugins = create_plugins(settings.enabled_plugins, context_for, discover)
    clock_sync = None
    if gateway.clock_sync:
        if ClockSync.supported(regmap):
            clock_sync = ClockSync(bus, regmap, state, ZoneInfo(gateway.timezone))
        else:
            logger.warning("clock sync is enabled but map %r has no clock registers", regmap.system)
    return Runtime(
        regmap, bus, state, events, poller, writer, plugins, clock_sync, gateway.heartbeat_file
    )


async def run_application(
    settings: AppSettings, stop: asyncio.Event, discover: EntryPoints = installed_plugins
) -> None:
    runtime = build_runtime(settings, discover)
    gateway = settings.gateway
    logger.info(
        "starting: gateway %s:%d, map %s (%s), read-only=%s, plugins=%s",
        gateway.host,
        gateway.port,
        runtime.regmap.system,
        gateway.language,
        gateway.read_only,
        ", ".join(runtime.plugins) or "none",
    )
    supervisor = Supervisor()
    supervisor.start("bus", runtime.bus.run)

    async def poll() -> None:
        await runtime.bus.connected.wait()
        await runtime.poller.run()

    supervisor.start("poller", poll)
    supervisor.start("writer", runtime.writer.run)
    for name, plugin in runtime.plugins.items():
        supervisor.start(f"plugin {name}", plugin.run)
    if runtime.clock_sync is not None:
        supervisor.start("clock sync", runtime.clock_sync.run)
    if runtime.heartbeat_file is not None:
        heartbeat_file = runtime.heartbeat_file
        supervisor.start("heartbeat", lambda: run_heartbeat(heartbeat_file))
    try:
        await stop.wait()
    finally:
        logger.info("stopping")
        await supervisor.stop()
