# Development

## Setup

```bash
uv venv --python 3.12 .venv
uv pip install -e ".[dev]"
```

| Task | Command |
|---|---|
| Tests | `.venv/bin/pytest -q` |
| Lint and format | `.venv/bin/ruff check src tests` and `.venv/bin/ruff format src tests` |
| Types | `.venv/bin/mypy` (strict, `src/`) |

CI runs the same three. The suite takes about half a minute because several tests run real time
bus cycles against the simulator.

## Project layout

```text
src/diematic/
  cli.py, app.py        command line and wiring
  config/               settings (environment > YAML > defaults)
  transport/            TCP transport, backoff
  modbus/               RTU framing, the bus and its windows
  registers/            map schema, loader, codecs, read planner
  core/                 poller, writer, state, events, supervisor, clock sync, heartbeat
  plugins/              plugin contract, loader, mqtt/
  sim/                  simulated boiler
  maps/                 shipped register maps
diematic_modbus/        Home Assistant app
docs/, config/, docker/ documentation, example configuration, Mosquitto test config
tests/                  unit tests per package, functional/ for the whole stack
```

## Test layers

- **Unit tests** per package, with fakes for the bus and the MQTT client.
- **Simulator tests** (`tests/modbus`, `tests/sim`) run the real bus against `BoilerSimulator`,
  which reproduces the talk/listen rhythm and counts requests that collide with the boiler's
  talking phase. A collision count of zero is asserted wherever timing matters.
- **End-to-end tests** (`tests/functional`) run the application, an embedded MQTT broker
  (`amqtt`) and an independent MQTT client playing Home Assistant: discovery, a setpoint change
  reaching the boiler, refusal of out-of-range values, thermostat modes, program editing and
  recovery after a broker restart.

Timings in those tests are shortened through the bus settings; a mismatch between the bus's
`master_phase` and the simulator's talking phase makes every request collide, exactly as it would
with a real boiler ([protocol](protocol.md)).

## The simulator

```bash
diematic simulate --map diematic_isystem --port 5020 --master-phase 5 --silent-phase 5
DIEMATIC_HOST=127.0.0.1 DIEMATIC_PORT=5020 diematic run
```

It serves a plausible value for every register of the map. With Docker Compose use
`--profile sim` and `DIEMATIC_HOST=simulator`, `DIEMATIC_PORT=5020`; add `--profile broker`
for a Mosquitto.

## Contributing

Keep commits small, each one logical change with a message that says why. Names and code are in
English; comments are reserved for behaviour or decisions that the code does not explain by
itself. Tests go in the same commit as the code they cover.
