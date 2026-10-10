# Home Assistant app (HAOS)

The `diematic_modbus/` folder is a Home Assistant app (formerly add-on). It maps its options and
the Supervisor's MQTT service onto the settings of the bridge, so with the Mosquitto app
installed the only thing to enter is the converter's address.

## Installing

1. In Home Assistant open *Settings > Apps > App store > Repositories* and add the URL of this
   repository, then install **Diematic Modbus**.
2. Set `gateway_host` and `gateway_port`, leave the MQTT options empty to use the broker Home
   Assistant provides, and start the app.

The Supervisor pulls a prebuilt image (`image:` in `config.yaml`); it does not build it. Local
development of the app therefore means building the image yourself, see below.

## Options

| Option | Maps to | Notes |
|---|---|---|
| `gateway_host`, `gateway_port` | `DIEMATIC_HOST`, `DIEMATIC_PORT` | Required. |
| `map` | `DIEMATIC_MAP` | `diematic_isystem`, `diematic3_legacy`, or a `.yaml` file name from the app's configuration folder (`/config`). |
| `language` | `DIEMATIC_LANGUAGE` | `pl` or `en`. |
| `slave_address` | `DIEMATIC_SLAVE_ADDRESS` | Optional. |
| `disabled_registers` | `DIEMATIC_DISABLED_REGISTERS` | Optional, comma separated. See [register maps](register-maps.md#leaving-registers-out). |
| `read_only`, `trailing_zero`, `clock_sync` | matching `DIEMATIC_*` | |
| `fast_interval`, `slow_interval` | matching `DIEMATIC_*` | Seconds. |
| `log_level` | `DIEMATIC_LOG_LEVEL` | |
| `mqtt_host`, `mqtt_port`, `mqtt_username`, `mqtt_password` | `MQTT_*` | Optional; empty uses the Supervisor's MQTT service. |
| `mqtt_topic_prefix`, `mqtt_discovery_prefix` | `MQTT_TOPIC_PREFIX`, `MQTT_DISCOVERY_PREFIX` | |
| `mqtt_boiler_availability` | `MQTT_BOILER_AVAILABILITY` | Off by default. |

The time zone of the boiler clock (the *Boiler time* sensor and `clock_sync`) is Home Assistant's:
the Supervisor passes it to the app as `TZ`, and the bridge uses it because the app sets no zone of
its own. `diematic_modbus/run.sh` does the translation of the options; a test compares its variable
names, the options and the translations with the real settings, so they cannot drift apart.

## Custom register maps

Put a map in the app's configuration folder (reachable through the Samba or SSH apps under
`addon_configs/…_diematic_modbus/`) and set `map` to its file name, for example `my_system.yaml`.

## Publishing

The repository URL and image name point at `jakubkosinski` on GitHub. Before the first release:

1. Make sure the tag matches `version` in `diematic_modbus/config.yaml`, then push `vX.Y.Z`.
   `.github/workflows/app-image.yml` builds the image for amd64 and arm64, checks the version and
   publishes it to GitHub Container Registry. Make the package public so Home Assistant can pull it.
2. Bump `version` and `CHANGELOG.md` for every release; Home Assistant offers the update when the
   version changes.

The workflows have not been run yet.

To test an image locally, build it from the repository root (the build needs `src/`):

```bash
docker build -f diematic_modbus/Dockerfile -t diematic-modbus-app .
```
