"""Command line interface: run the service, probe the bus, validate maps, run a simulator."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from diematic import __version__
from diematic.app import run_application
from diematic.config.settings import AppSettings, ConfigError, load_settings
from diematic.core.heartbeat import heartbeat_age
from diematic.modbus.bus import BusError, DiematicBus
from diematic.modbus.rtu import ModbusException
from diematic.plugins.base import PluginError
from diematic.registers.loader import MapError, RegisterMap, load_map
from diematic.registers.planner import plan_blocks
from diematic.sim.boiler import BoilerSimulator
from diematic.sim.defaults import default_registers
from diematic.transport.tcp import TcpTransport

HEARTBEAT_ENV = "DIEMATIC_HEARTBEAT_FILE"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level, format=LOG_FORMAT, stream=sys.stderr, force=True)


def _stop_on_signals(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop.set)


async def _serve(settings: AppSettings) -> None:
    stop = asyncio.Event()
    _stop_on_signals(stop)
    await run_application(settings, stop)


def cmd_run(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    configure_logging(settings.gateway.log_level)
    asyncio.run(_serve(settings))
    return 0


async def probe(
    settings: AppSettings, start: int, end: int, chunk: int, wait: float, connect_wait: float = 15.0
) -> int:
    """Print raw register words for a range, annotated with the map's names and values."""
    gateway = settings.gateway
    regmap = load_map(gateway.map, gateway.language)
    transport = TcpTransport(gateway.host, gateway.port, gateway.connect_timeout)
    bus = DiematicBus(
        transport,
        gateway.slave_address or regmap.slave_address,
        gateway.bus.timing(),
        gateway.trailing_zero,
    )
    task = asyncio.create_task(bus.run())
    try:
        try:
            await asyncio.wait_for(bus.connected.wait(), connect_wait)
        except TimeoutError:
            print(f"error: cannot connect to {gateway.host}:{gateway.port}", file=sys.stderr)
            return 1
        words: dict[int, int | None] = {}
        for first in range(start, end + 1, chunk):
            count = min(chunk, end - first + 1)
            await _read_range(bus, first, count, wait, words)
        _print_probe(regmap, words)
        return 0
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _read_range(
    bus: DiematicBus, first: int, count: int, wait: float, words: dict[int, int | None]
) -> None:
    try:
        values = await bus.read_registers(first, count, max_wait=wait)
    except ModbusException as exc:
        if count > 1:
            for address in range(first, first + count):
                await _read_range(bus, address, 1, wait, words)
            return
        print(f"{first:5d}  {exc}", file=sys.stderr)
        words[first] = None
    except BusError as exc:
        print(f"{first:5d}  {exc}", file=sys.stderr)
        words[first] = None
    else:
        words.update(zip(range(first, first + count), values, strict=True))


def _print_probe(regmap: RegisterMap, words: dict[int, int | None]) -> None:
    starts = {r.address: r for r in regmap.registers}
    print("address  hex     dec    key / decoded value")
    for address in sorted(words):
        word = words[address]
        if word is None:
            print(f"{address:5d}    n/a")
            continue
        note = ""
        register = starts.get(address)
        if register is not None:
            span = [words.get(address + i) for i in range(register.width)]
            if all(w is not None for w in span):
                note = (
                    f"{register.key} = {register.codec.decode([w for w in span if w is not None])}"
                )
            else:
                note = register.key
        print(f"{address:5d}    0x{word:04X}  {word:5d}  {note}")


def cmd_probe(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    configure_logging("WARNING")
    if args.end < args.start:
        print("error: --to must not be lower than --from", file=sys.stderr)
        return 2
    return asyncio.run(probe(settings, args.start, args.end, args.chunk, args.wait))


def validate_map(source: str, language: str) -> int:
    try:
        regmap = load_map(source, language)
    except MapError as exc:
        print(f"invalid map: {exc}", file=sys.stderr)
        return 1
    writable = sum(r.is_writable for r in regmap.registers)
    print(
        f"{regmap.name} ({regmap.system}): {len(regmap.registers)} registers, {writable} writable"
    )
    for group in ("fast", "slow"):
        members = [r for r in regmap.registers if r.poll_group == group]
        blocks = plan_blocks(members, regmap.max_block, regmap.max_gap)
        print(f"  {group}: {len(members)} registers in {len(blocks)} read blocks")
    print(f"  derived sensors: {len(regmap.derived)}")
    return 0


def cmd_validate_map(args: argparse.Namespace) -> int:
    return validate_map(args.map, args.language)


async def simulate(
    regmap: RegisterMap,
    host: str,
    port: int,
    master_phase: float,
    silent_phase: float,
    stop: asyncio.Event,
    ready: asyncio.Future[int] | None = None,
) -> None:
    simulator = BoilerSimulator(
        registers=default_registers(regmap),
        address=regmap.slave_address,
        master_phase=master_phase,
        silent_phase=silent_phase,
    )
    bound = await simulator.start(host, port)
    if ready is not None:
        ready.set_result(bound)
    print(f"simulating {regmap.name} on {host}:{bound} (Ctrl-C to stop)", file=sys.stderr)
    try:
        await stop.wait()
    finally:
        await simulator.stop()


async def _simulate_until_signal(args: argparse.Namespace) -> None:
    stop = asyncio.Event()
    _stop_on_signals(stop)
    regmap = load_map(args.map, args.language)
    await simulate(regmap, args.host, args.port, args.master_phase, args.silent_phase, stop)


def cmd_simulate(args: argparse.Namespace) -> int:
    configure_logging("INFO")
    asyncio.run(_simulate_until_signal(args))
    return 0


def healthcheck(path: Path | None, max_age: float) -> int:
    if path is None:
        print("no heartbeat file configured", file=sys.stderr)
        return 1
    age = heartbeat_age(path)
    if age is None or age > max_age:
        print("heartbeat is missing or stale", file=sys.stderr)
        return 1
    return 0


def cmd_healthcheck(args: argparse.Namespace) -> int:
    configured = args.file or os.environ.get(HEARTBEAT_ENV)
    return healthcheck(Path(configured) if configured else None, args.max_age)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="diematic", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run the service")
    run.add_argument("--config", help="YAML configuration file (or set DIEMATIC_CONFIG)")
    run.set_defaults(handler=cmd_run)

    probe_cmd = commands.add_parser("probe", help="dump a register range from the boiler")
    probe_cmd.add_argument("--config")
    probe_cmd.add_argument("--from", dest="start", type=int, required=True)
    probe_cmd.add_argument("--to", dest="end", type=int, required=True)
    probe_cmd.add_argument("--chunk", type=int, default=20, help="registers per request")
    probe_cmd.add_argument("--wait", type=float, default=60.0, help="seconds to wait per request")
    probe_cmd.set_defaults(handler=cmd_probe)

    check = commands.add_parser("validate-map", help="check a register map file or built-in map")
    check.add_argument("map", help="built-in map name or path to a YAML file")
    check.add_argument("--language", default="en", choices=["pl", "en"])
    check.set_defaults(handler=cmd_validate_map)

    sim = commands.add_parser("simulate", help="run a simulated boiler for development")
    sim.add_argument("--map", default="diematic_isystem")
    sim.add_argument("--language", default="en", choices=["pl", "en"])
    sim.add_argument("--host", default="127.0.0.1")
    sim.add_argument("--port", type=int, default=5020)
    sim.add_argument("--master-phase", type=float, default=5.0)
    sim.add_argument("--silent-phase", type=float, default=5.0)
    sim.set_defaults(handler=cmd_simulate)

    health = commands.add_parser("healthcheck", help="exit 0 if the heartbeat file is fresh")
    health.add_argument("--file", help=f"heartbeat file (default: ${HEARTBEAT_ENV})")
    health.add_argument("--max-age", type=float, default=60.0)
    health.set_defaults(handler=cmd_healthcheck)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (ConfigError, MapError, PluginError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
