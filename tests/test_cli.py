import asyncio
import os
import time

import pytest

from diematic import cli
from diematic.config.settings import AppSettings, BusSettings, GatewaySettings
from diematic.modbus.rtu import FrameKind, FrameSplitter
from diematic.registers.loader import load_map
from diematic.transport.tcp import TcpTransport

TINY_MAP = """
system: tiny
name: Tiny test system
registers:
  - {key: outdoor_temp, address: 601, type: temp, kind: measure, name: Outdoor}
  - key: setpoint
    address: 650
    type: temp
    kind: parameter
    name: Setpoint
    min: 5
    max: 30
    step: 0.5
  - key: mode
    address: 651
    type: enum
    kind: parameter
    name: Mode
    values: {0: "off", 4: day}
"""

FAST_BUS = BusSettings(
    silence_gap=0.1, master_phase=0.25, cycle=0.9, cycle_guard=0.05, window=0.5, idle_open_after=0.7
)


@pytest.fixture
def map_file(tmp_path):
    path = tmp_path / "tiny.yaml"
    path.write_text(TINY_MAP, encoding="utf-8")
    return path


def test_validate_map_summarises_a_built_in_map(capsys):
    assert cli.main(["validate-map", "diematic_isystem"]) == 0
    output = capsys.readouterr().out
    assert "diematic_isystem" in output and "read blocks" in output


def test_validate_map_accepts_a_file(map_file, capsys):
    assert cli.main(["validate-map", str(map_file), "--language", "pl"]) == 0
    assert "3 registers, 2 writable" in capsys.readouterr().out


def test_validate_map_reports_problems_without_a_traceback(tmp_path, capsys):
    broken = tmp_path / "broken.yaml"
    broken.write_text("system: x\nname: x\nregisters:\n  - {key: a, address: 1}\n")
    assert cli.main(["validate-map", str(broken)]) == 1
    assert "invalid map" in capsys.readouterr().err


def test_unknown_map_name_is_reported(capsys):
    assert cli.main(["validate-map", "no_such_map"]) == 1
    assert "not found" in capsys.readouterr().err


def test_configuration_errors_exit_with_status_2(tmp_path, capsys):
    assert cli.main(["run", "--config", str(tmp_path / "missing.yaml")]) == 2
    assert capsys.readouterr().err.startswith("error:")


def test_probe_rejects_an_inverted_range(tmp_path, capsys, monkeypatch):
    config = tmp_path / "c.yaml"
    config.write_text("gateway: {host: h, port: 1}\n")
    assert cli.main(["probe", "--config", str(config), "--from", "10", "--to", "5"]) == 2
    assert "--to" in capsys.readouterr().err


def test_healthcheck_depends_on_the_heartbeat_age(tmp_path, monkeypatch):
    beat = tmp_path / "beat"
    monkeypatch.delenv(cli.HEARTBEAT_ENV, raising=False)
    assert cli.main(["healthcheck"]) == 1
    assert cli.main(["healthcheck", "--file", str(beat)]) == 1
    beat.write_text("x")
    assert cli.main(["healthcheck", "--file", str(beat)]) == 0
    os.utime(beat, (time.time() - 300, time.time() - 300))
    assert cli.main(["healthcheck", "--file", str(beat), "--max-age", "60"]) == 1
    monkeypatch.setenv(cli.HEARTBEAT_ENV, str(beat))
    assert cli.main(["healthcheck", "--max-age", "600"]) == 0


async def start_simulator(map_file, master=0.3, silent=0.6):
    regmap = load_map(map_file)
    stop = asyncio.Event()
    ready = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(cli.simulate(regmap, "127.0.0.1", 0, master, silent, stop, ready))
    port = await asyncio.wait_for(ready, 2)
    return regmap, port, stop, task


async def test_simulate_serves_a_boiler_with_default_values(map_file):
    regmap, port, stop, task = await start_simulator(map_file)
    transport = TcpTransport("127.0.0.1", port)
    await transport.open()
    splitter = FrameSplitter()
    master_seen = False
    while not master_seen:
        master_seen = any(
            f.kind is FrameKind.READ_REQUEST for f in splitter.feed(await transport.read(1))
        )
    await transport.close()
    stop.set()
    await asyncio.wait_for(task, 2)
    assert master_seen and regmap.get("setpoint").address == 650


async def test_probe_prints_words_with_decoded_values(map_file, capsys):
    _, port, stop, task = await start_simulator(map_file)
    gateway = GatewaySettings(
        host="127.0.0.1", port=port, map=str(map_file), bus=FAST_BUS, language="en"
    )
    status = await cli.probe(AppSettings(gateway, []), 649, 652, chunk=2, wait=5)
    stop.set()
    await asyncio.wait_for(task, 2)
    output = capsys.readouterr()
    assert status == 0
    assert "setpoint = 17.5" in output.out
    assert "mode = off" in output.out
    assert "n/a" in output.out
    assert "649" in output.out


async def test_probe_reports_an_unreachable_gateway(map_file, capsys):
    gateway = GatewaySettings(host="127.0.0.1", port=1, map=str(map_file), bus=FAST_BUS)
    status = await cli.probe(AppSettings(gateway, []), 600, 601, chunk=2, wait=1, connect_wait=0.3)
    assert status == 1
    assert "cannot connect" in capsys.readouterr().err


def test_parser_requires_a_command():
    with pytest.raises(SystemExit):
        cli.main([])
