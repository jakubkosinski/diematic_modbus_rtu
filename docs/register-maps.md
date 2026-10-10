# Register maps

A register map describes a system's registers: address, type, units, limits, labels and how each
one is exposed. Nothing about a particular boiler is hard-coded. Two maps ship in
`src/diematic/maps/`:

| Map | For |
|---|---|
| `diematic_isystem` | Diematic iSystem, following the manufacturer's parameter list for the GTW-26 gateway. |
| `diematic3_legacy` | The older Diematic 3 register layout (taken from the community reference implementation). |

Select one with `DIEMATIC_MAP`, or point it at your own file (`/config/my_system.yaml`).

```bash
diematic validate-map diematic_isystem     # summary and read plan
diematic validate-map my_system.yaml --language en
```

## File format

```yaml
system: my_system              # identifier
name: My boiler system
slave_address: 10              # default Modbus address
max_block: 40                  # most registers per read (the GTW-26 allows 40)
max_gap: 0                     # unmapped registers a read may bridge

templates: {}                  # free-form: YAML anchors to reuse attributes

registers: []                  # see below
time_programs: []              # weekly programs
derived: []                    # sums of other registers
composites: []                 # thermostats built from registers
```

### Registers

```yaml
- key: circuit_a_day_temp      # unique, lower case; becomes the MQTT topic
  address: 650
  type: temp                   # temp | uint | enum | bits | time_program
  kind: parameter              # parameter (writable) | measure (read only)
  name: {pl: Temperatura dzień obiegu A, en: Circuit A day temperature}
  unit: "°C"
  min: 5                       # limits are in engineering units, not raw tenths
  max: 30
  step: 0.5
  poll: fast                   # fast | slow; default fast for measures, slow for parameters
  device_class: temperature    # Home Assistant
  state_class: measurement
  group: circuit_a             # drives entity categories
  enabled_by_default: true     # false: the entity is created disabled in Home Assistant
```

| Field | Notes |
|---|---|
| `type: temp` | Sign-magnitude, scale 0.1, `0xFFFF` unavailable. |
| `type: uint` | `scale` (default 1) and `invalid: [raw values]`. |
| `type: enum` | `values: {raw: label}`; unknown raw values read as no value. |
| `type: bits` | `bits: {flag: {bit: 3, name: ..., writable: true}}`. Flags become binary sensors or switches. |
| `writable` | Defaults to `kind: parameter`. A measure cannot be writable. |
| `name`, enum labels, bit names | Plain text, or `{pl: ..., en: ...}`. The configured language is used, English as fallback. |

Writes are validated: a number outside `min`..`max` is **rejected** (not clamped) and one that is
off the `step` is rounded to the nearest step.

### Weekly programs

```yaml
time_programs:
  - {key: circuit_a_program, name: {pl: Program obiegu A, en: Circuit A program}, address: 126}
```

expands to seven registers `circuit_a_program_monday` … `_sunday`, three words each, at
`address + 3 * day`. They are text entities such as `07:00-20:00,22:00-23:30`.

### Derived sensors

```yaml
derived:
  - key: burner_hours
    name: {pl: Godziny pracy palnika, en: Burner operating hours}
    terms: [{key: burner_hours_tens, factor: 10}, {key: burner_hours_units}]
    unit: h
```

The value is the weighted sum once every term is known. Use it for counters split over registers.

### Thermostats

```yaml
composites:
  - key: circuit_a
    name: {pl: Obieg A, en: Circuit A}
    current_temperature: circuit_a_room_temp
    low: circuit_a_night_temp         # target_temp_low
    high: circuit_a_day_temp          # target_temp_high
    mode_register: circuit_a_forcing  # a bits register
    modes: {auto: auto, heat: day, "off": frost_protection}   # Home Assistant mode -> flag
    presets: {night: night, day: day}                         # preset -> flag
    exclusive: [frost_protection, night, day, auto]           # cleared when a mode is chosen
```

Choosing a mode sets its flag, clears the other `exclusive` flags and leaves every other flag of
the register untouched. The loader checks that all referenced registers and flags exist and are
writable.

### YAML pitfall

YAML reads `off`, `on`, `yes` and `no` as booleans. Quote them when they are labels or keys:
`{pl: Wyłączony, en: "Off"}`, `"off": frost_protection`. The loader reports a non-text label
instead of silently accepting it.

## Verifying a map on your boiler

The shipped iSystem map follows the GTW-26 gateway documentation. If your converter sits
directly on the regulator's bus the addresses may differ, so compare before you trust writes.
`probe` prints raw words next to the map's decoded values:

```bash
diematic probe --from 600 --to 700 --config config.yaml
```

Things worth checking on the first run, because the manufacturer's list leaves them open:

- the sign and range of a few temperatures against the regulator's display;
- time programs: that a set bit really is the day period;
- `anticipation` (read here in tenths of an hour, with 10.1 meaning off);
- the stop-period months (read as 1-12);
- the thermostat flags: that setting `day`, `night`, `auto` and `frost_protection` in the
  forcing register does what the regulator's display shows;
- the solar registers, which have no documented units and are exposed raw and disabled.

Start with `DIEMATIC_READ_ONLY=true` while you compare.

## Adding a system

1. Copy a shipped map and adjust addresses, types and labels.
2. `diematic validate-map my_system.yaml` until it passes.
3. Try it against `diematic simulate --map my_system.yaml`, then against the boiler with
   `diematic probe`.
4. Point `DIEMATIC_MAP` at it. For a container, mount the file and use its path.

A data type the codecs do not cover needs a new class in `registers/codecs.py`.
