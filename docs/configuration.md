# Configuration

Settings come from three layers; the first one that sets a value wins:

1. **Environment variables** (also from an `.env` file via Docker Compose). Blank values count as unset.
2. **A YAML file**, given with `diematic run --config FILE` or `DIEMATIC_CONFIG`.
3. **Defaults.**

The YAML file has two sections: `gateway` and `plugins` (one entry per plugin). See
[`config/config.example.yaml`](../config/config.example.yaml) for a commented example. Unknown
keys are rejected, so typos surface at startup, and error messages never print the offending
value.

## Gateway (`DIEMATIC_*`)

| YAML key (`gateway.`) | Environment variable | Default | Meaning |
|---|---|---|---|
| `host` | `DIEMATIC_HOST` | required | Converter address. |
| `port` | `DIEMATIC_PORT` | required | Converter TCP port. |
| `map` | `DIEMATIC_MAP` | `diematic_isystem` | Built-in map name or path to a YAML file. |
| `disabled_registers` | `DIEMATIC_DISABLED_REGISTERS` | none | Registers, derived sensors, thermostats or groups to leave out, as a YAML list or a comma separated variable. See [register maps](register-maps.md#leaving-registers-out). |
| `language` | `DIEMATIC_LANGUAGE` | `pl` | `pl` or `en`: entity names and enum values. |
| `slave_address` | `DIEMATIC_SLAVE_ADDRESS` | from the map (10) | Regulator Modbus address, 1-247. |
| `read_only` | `DIEMATIC_READ_ONLY` | `false` | Refuse every write. |
| `trailing_zero` | `DIEMATIC_TRAILING_ZERO` | `true` | Extra zero byte after each frame. |
| `connect_timeout` | `DIEMATIC_CONNECT_TIMEOUT` | `5` | Seconds to wait for the TCP connection. |
| `fast_interval` | `DIEMATIC_FAST_INTERVAL` | `30` | Seconds between polls of the `fast` group. |
| `slow_interval` | `DIEMATIC_SLOW_INTERVAL` | `300` | Seconds between polls of the `slow` group. |
| `offline_after` | `DIEMATIC_OFFLINE_AFTER` | `120` | Seconds without a valid answer before the boiler counts as offline (minimum 10). |
| `clock_sync` | `DIEMATIC_CLOCK_SYNC` | `false` | Set the boiler clock when it drifts. |
| `timezone` | `DIEMATIC_TIMEZONE` | `UTC` | Time zone the boiler clock is set to (IANA name, e.g. `Europe/Warsaw`). The *Boiler time* sensor and `clock_sync` use it; with the wrong zone the time is shifted. |
| `heartbeat_file` | `DIEMATIC_HEARTBEAT_FILE` | none | File touched every 10 s for health checks. |
| `log_level` | `DIEMATIC_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `bus.*` | `DIEMATIC_BUS__*` | see [protocol](protocol.md) | `silence_gap`, `master_phase`, `cycle`, `cycle_guard`, `window`, `idle_open_after`, `response_timeout`. |

`DIEMATIC_ENABLED_PLUGINS` (comma separated) overrides which plugins run. Without it the
plugins listed under `plugins:` in the YAML file run, and with neither the MQTT plugin runs.
`DIEMATIC_CONFIG` names the YAML file.

Poll groups: measurements are `fast`, settings and programs `slow`; a register can override that
with `poll:` in the map. After a cycle with link problems the next one starts after 10 s instead
of the normal interval.

## MQTT plugin (`MQTT_*`, or `plugins.mqtt.*`)

| YAML key | Environment variable | Default | Meaning |
|---|---|---|---|
| `host` | `MQTT_HOST` | `localhost` | Broker address. |
| `port` | `MQTT_PORT` | `1883` | Broker port. |
| `username` | `MQTT_USERNAME` | none | |
| `password` | `MQTT_PASSWORD` | none | Kept as a secret; never logged. |
| `client_id` | `MQTT_CLIENT_ID` | `diematic` | |
| `tls` | `MQTT_TLS` | `false` | Use TLS. |
| `tls_ca` | `MQTT_TLS_CA` | system CAs | CA certificate file. |
| `tls_insecure` | `MQTT_TLS_INSECURE` | `false` | Skip host name verification (not recommended). |
| `keepalive` | `MQTT_KEEPALIVE` | `60` | Seconds. |
| `qos` | `MQTT_QOS` | `1` | 0, 1 or 2. |
| `topic_prefix` | `MQTT_TOPIC_PREFIX` | `diematic` | No wildcards, spaces or empty levels. |
| `boiler_availability` | `MQTT_BOILER_AVAILABILITY` | `false` | Make entities unavailable while the boiler is offline. By default they keep their last value and only the *Boiler connection* sensor shows the outage. |
| `discovery` | `MQTT_DISCOVERY` | `true` | Publish Home Assistant discovery. |
| `discovery_prefix` | `MQTT_DISCOVERY_PREFIX` | `homeassistant` | Must match Home Assistant. |
| `node_id` | `MQTT_NODE_ID` | `diematic` | Identifies the device; letters, digits, `_`, `-`. |
| `device_name` | `MQTT_DEVICE_NAME` | `Diematic boiler` | Device name in Home Assistant. |

## Secrets

Prefer `MQTT_PASSWORD` in the environment (or the Docker/HA secret mechanism) over writing it
into a YAML file. Neither the password nor rejected values appear in logs or error messages.

## Logging

Logs go to standard error. `INFO` reports start-up and availability changes; `WARNING` covers
problems the service works around, including rejected commands and failed reads; `DEBUG` adds
the hex of every frame the service sends. Passwords are never logged at any level.
