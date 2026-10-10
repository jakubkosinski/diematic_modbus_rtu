# MQTT and Home Assistant

The MQTT plugin publishes every value, accepts commands and announces all entities to Home
Assistant through MQTT discovery. In Home Assistant you only need the MQTT integration; the
entities appear under one device ("Diematic boiler" by default).

## Topics

`<prefix>` is `MQTT_TOPIC_PREFIX` (default `diematic`), `<key>` a register key from the map.

| Topic | Direction | Payload |
|---|---|---|
| `<prefix>/status` | out, retained | `online` / `offline` (last will): the bridge itself. |
| `<prefix>/boiler_status` | out, retained | `online` / `offline`: whether the boiler has answered recently (see `DIEMATIC_OFFLINE_AFTER`). |
| `<prefix>/<key>` | out, retained | The value as text. No value (for example an absent room sensor) is an empty payload. |
| `<prefix>/<key>/set` | in | A new value for a writable register. |
| `<prefix>/<register>/<flag>` | out, retained | `ON` / `OFF` for each flag of a flags register. |
| `<prefix>/<register>/<flag>/set` | in | `ON` / `OFF` (also `1`/`0`, `true`/`false`). |
| `<prefix>/<register>/set` | in | For a flags register: a JSON object such as `{"night": true, "auto": false}`. |
| `<prefix>/climate/<circuit>/mode` and `/preset` | out, retained | Thermostat state. |
| `<prefix>/climate/<circuit>/mode/set` and `/preset/set` | in | `auto` / `heat` / `off`, and `night` / `day`. |
| `<prefix>/set_result` | out | JSON `{"key": ..., "ok": true/false, "message": ...}` for every command. |
| `<discovery_prefix>/<component>/<node_id>/<object>/config` | out, retained | Home Assistant discovery. |

Value formats: numbers without trailing zeros (`21.5`, `22`), enums as their label in the
configured language, weekly programs as `HH:MM-HH:MM[,HH:MM-HH:MM...]` (the day periods; empty
means none). Commands accept a decimal comma. Payloads over 1 KiB, invalid UTF-8, `nan`/`inf`,
unknown registers and unknown flags are ignored with a warning.

## Entities

| Register | Home Assistant entity |
|---|---|
| Measurement (number) | `sensor` with unit, device class and state class |
| Measurement (enum) | `sensor` with `device_class: enum` and its options |
| Setting (number) | `number` with the map's min, max and step |
| Setting (enum) | `select` |
| Flag in a writable register | `switch` |
| Flag in a read-only register | `binary_sensor` |
| Weekly program | `text`, validated by a pattern, enabled by default |
| Derived value | `sensor` (energy totals, burner counters) |
| Boiler link | `binary_sensor` *Boiler connection* (diagnostic) |
| Date and time of the boiler | `sensor` *Boiler time* with `device_class: timestamp` |
| Heating circuit | `climate` with night/day setpoints, `auto`/`heat`/`off` and `night`/`day` presets |

Helper registers (split counters, the unit-less solar values, stop periods) are created disabled;
enable them in Home Assistant when needed. Settings of the installation and service groups get
the *configuration* entity category, service measurements the *diagnostic* one. Circuits B and C
are disabled by default because many installations do not have them.

Entities keep their last value while the boiler does not answer: they only become unavailable
when the bridge itself is gone (`<prefix>/status` is `offline`). The state of the boiler is shown
by the diagnostic **Boiler connection** binary sensor, which follows `<prefix>/boiler_status`.
Set `MQTT_BOILER_AVAILABILITY=true` to make every entity unavailable while the boiler is offline
as well.

### Boiler time

The *Boiler time* sensor combines the boiler's date and time registers into one moment, for
example `2026-07-15T10:30:00+02:00`. The boiler stores no time zone, so the one from `timezone`
([configuration](configuration.md)) is applied, including the summer and winter offset. Compare
it with Home Assistant's own time to see whether the boiler clock has drifted, or turn on
`clock_sync` to have it corrected. It refreshes at the fast poll interval and is unknown while a
date is incomplete or impossible.

### The thermostat

The thermostat has no setpoint of its own: its two targets are the circuit's night and day
temperatures. Choosing a mode sets one flag of the circuit's forcing register and clears the
other mode flags, leaving the DHW bits alone:

| Home Assistant | Flag set |
|---|---|
| mode `auto` | `auto` |
| mode `heat` | `day` |
| mode `off` | `frost_protection` (anti-freeze) |
| preset `night` / `day` | `night` / `day` |

## Examples

```bash
# Day temperature of circuit A to 21.5 degrees
mosquitto_pub -t diematic/circuit_a_day_temp/set -m 21.5

# Return circuit A to the weekly program
mosquitto_pub -t diematic/climate/circuit_a/mode/set -m auto

# Weekly program for Monday on circuit A
mosquitto_pub -t diematic/circuit_a_program_monday/set -m "06:00-08:00,17:00-22:30"

# Watch results and state
mosquitto_sub -v -t 'diematic/#'
```

A Home Assistant automation that lowers the night temperature when nobody is home:

```yaml
automation:
  - alias: Lower the night temperature while away
    triggers:
      - trigger: state
        entity_id: group.family
        to: not_home
    actions:
      - action: number.set_value
        target:
          entity_id: number.diematic_circuit_a_night_temp
        data:
          value: 16
```

## Behaviour to know about

- **Latency.** The boiler only listens for part of every ten seconds, so a command takes between
  about one and ten seconds to show up, then is confirmed by read-back and published.
- **Rejections.** A value outside the map's limits is refused and reported on `set_result`; it
  is not clamped.
- **Home Assistant restarts.** The plugin watches `<discovery_prefix>/status` and announces
  everything again when Home Assistant comes back.
- **Broker outages.** The plugin reconnects with backoff and re-announces; the bridge keeps
  polling meanwhile.
- **Security.** Anyone who can publish to `<prefix>/+/set` can change boiler settings. Use broker
  authentication and TLS ([configuration](configuration.md)), or `DIEMATIC_READ_ONLY=true`.
