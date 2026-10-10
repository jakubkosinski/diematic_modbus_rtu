# Diematic Modbus

Connects a De Dietrich boiler with a Diematic iSystem regulator to Home Assistant. Sensors,
settings, weekly programs and thermostats appear automatically through MQTT discovery.

## Before you start

1. Wire an RS485-to-Ethernet converter to the boiler's Modbus port and configure it as a TCP
   server with the serial parameters of your bus. It must pass raw RTU frames through; do not
   enable Modbus TCP conversion.
2. Install the Mosquitto broker app (or have your own broker) and the MQTT integration.

## Configuration

| Option | Meaning |
|---|---|
| `gateway_host`, `gateway_port` | Address of the converter. Required. |
| `map` | `diematic_isystem` (default), `diematic3_legacy`, or the name of a YAML file placed in this app's configuration folder. |
| `language` | `pl` or `en`: language of entity names and enum values. |
| `slave_address` | Regulator address; empty uses the map's (10). |
| `disabled_registers` | Registers or groups your boiler does not have, comma separated (for example `solar,circuit_c,heat_pump`). They are not read and their entities are removed. |
| `read_only` | Refuse every change requested over MQTT. |
| `trailing_zero` | Extra zero byte after each frame; keep enabled. |
| `fast_interval`, `slow_interval` | Seconds between polls of measurements and of settings/programs. |
| `clock_sync` | Set the boiler clock when it drifts from Home Assistant's time. |
| `mqtt_host`, `mqtt_port`, `mqtt_username`, `mqtt_password` | Your own broker. Leave empty to use the broker Home Assistant provides. |
| `mqtt_topic_prefix`, `mqtt_discovery_prefix` | Topic layout; the discovery prefix must match Home Assistant's. |
| `mqtt_boiler_availability` | Make entities unavailable while the boiler is offline. Off by default: entities keep their last value and the *Boiler connection* sensor shows the outage. |

## How it behaves

The boiler talks on the bus for about five seconds and then listens for about five seconds, so
a change you make is applied within the next listening window and a command can take up to ten
seconds to show up. Every change is read back and the result is published on
`<prefix>/set_result`.

If the boiler or the broker becomes unreachable the app keeps running and reconnects; the
entities show as unavailable meanwhile.
