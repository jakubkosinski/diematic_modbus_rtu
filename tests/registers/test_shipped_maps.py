import pytest

from diematic.registers.loader import load_map

SHIPPED_MAPS = ["diematic_isystem", "diematic3_legacy"]


@pytest.mark.parametrize("language", ["en", "pl"])
@pytest.mark.parametrize("name", SHIPPED_MAPS)
def test_shipped_map_loads(name, language):
    loaded = load_map(name, language)
    assert loaded.registers
    assert all(r.name for r in loaded.registers)


def test_isystem_time_programs_follow_documented_layout():
    loaded = load_map("diematic_isystem")
    expected_first = {
        "circuit_a_program": 126,
        "circuit_b_program": 147,
        "circuit_c_program": 168,
        "dhw_program": 189,
        "aux_program": 210,
    }
    for prefix, first in expected_first.items():
        assert loaded.get(f"{prefix}_monday").address == first
        assert loaded.get(f"{prefix}_sunday").address == first + 18


@pytest.mark.parametrize(
    ("key", "address", "raw", "expected"),
    [
        ("circuit_a_day_temp", 650, 215, 21.5),
        ("circuit_a_room_temp", 614, 0x8005, -0.5),
        ("circuit_a_room_temp", 614, 0xFFFF, None),
        ("circuit_a_mode", 637, 4, "Day"),
    ],
)
def test_isystem_spot_checks(key, address, raw, expected):
    register = load_map("diematic_isystem").get(key)
    assert register.address == address
    assert register.codec.decode([raw]) == expected


def test_isystem_setpoint_limits_match_documentation():
    register = load_map("diematic_isystem").get("circuit_a_day_temp")
    assert (register.min, register.max, register.step) == (5, 30, 0.5)
    assert register.is_writable


def test_isystem_stop_periods_follow_documented_layout():
    loaded = load_map("diematic_isystem")
    assert loaded.get("stop_period_1_start_day").address == 309
    assert loaded.get("stop_period_10_end_month").address == 348
    assert loaded.get("stopped_circuits_1").address == 235
    assert loaded.get("stopped_circuits_10").address == 244
    assert loaded.get("stop_period_3_start_month").codec.decode([12]) == "December"


@pytest.mark.parametrize(
    ("key", "raw", "expected"),
    [
        ("water_pressure", 15, 1.5),
        ("ionization_current", 123, 12.3),
        ("boiler_flow_temp", 0x8000 | 55, -5.5),
        ("boiler_type", 23, "MIT"),
        ("season_mode", 5, "Winter"),
        ("phone_input_action", 16, "AUX"),
    ],
)
def test_isystem_measure_decoding(key, raw, expected):
    assert load_map("diematic_isystem").get(key).codec.decode([raw]) == expected


def test_isystem_bit_registers_name_documented_outputs():
    loaded = load_map("diematic_isystem")
    outputs = loaded.get("secondary_outputs").codec.decode([1 << 13 | 1 << 10])
    assert outputs["phone_output"] and outputs["aux_pump"] and not outputs["dhw_pump"]
    assert loaded.get("boiler_states").codec.decode([1 << 15])["cascade_fault"]


def test_isystem_raw_helper_registers_are_disabled_by_default():
    loaded = load_map("diematic_isystem")
    for key in ("heating_energy_kwh", "burner_hours_tens", "solar_collector_temp"):
        assert not loaded.get(key).enabled_by_default
    assert loaded.get("outdoor_temp").enabled_by_default


def test_isystem_derived_sensors_reference_existing_registers():
    loaded = load_map("diematic_isystem")
    derived = {d.key: d for d in loaded.derived}
    assert {t.key for t in derived["burner_starts"].terms} == {
        "burner_starts_tens",
        "burner_starts_units",
    }
    assert derived["heating_electrical_energy"].unit == "kWh"


def test_isystem_every_writable_numeric_register_has_limits():
    for register in load_map("diematic_isystem").registers:
        if register.is_writable and register.type in {"temp", "uint"}:
            assert register.min is not None and register.max is not None, register.key
            assert register.step is not None, register.key


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0xFFFF, "No error"),
        (0x0000, "D3: Flow sensor B fault"),
        (0x080F, "B16: False flame"),
        (0x1017, "No water 250"),
        (0x1814, "L250: No water"),
        (0x2019, "L32: Lockout: user reset"),
    ],
)
def test_isystem_error_codes_cover_every_boiler_family(raw, expected):
    assert load_map("diematic_isystem").get("boiler_error").codec.decode([raw]) == expected


def test_isystem_unknown_error_code_decodes_to_none():
    assert load_map("diematic_isystem").get("boiler_error").codec.decode([0x0F00]) is None


def test_legacy_mode_bits_reproduce_documented_mode_values():
    codec = load_map("diematic3_legacy").get("circuit_a_mode").codec
    auto = codec.decode([8])
    assert auto["auto"] and not auto["day"]
    day_by_temperature = codec.decode([36])
    assert day_by_temperature["day"] and day_by_temperature["circuit_temperature_controlled"]
    dhw_by_temperature = codec.decode([0x50])
    assert dhw_by_temperature["dhw_constant"] and dhw_by_temperature["dhw_temperature_controlled"]


def test_legacy_mode_change_keeps_dhw_bits():
    codec = load_map("diematic3_legacy").get("circuit_a_mode").codec
    current = 0x50 | 0x08
    changed = codec.apply(current, {"auto": False, "night": True})
    assert changed == 0x50 | 0x02


def test_legacy_reads_in_large_blocks():
    loaded = load_map("diematic3_legacy")
    assert (loaded.max_block, loaded.max_gap) == (64, 8)
    assert load_map("diematic_isystem").max_block == 40


@pytest.mark.parametrize(
    ("name", "circuits"),
    [
        ("diematic_isystem", {"circuit_a", "circuit_b", "circuit_c"}),
        ("diematic3_legacy", {"circuit_a", "circuit_b"}),
    ],
)
def test_shipped_maps_declare_a_thermostat_per_circuit(name, circuits):
    loaded = load_map(name)
    assert {c.key for c in loaded.composites} == circuits
    first = loaded.composites[0]
    assert first.enabled_by_default
    assert all(not c.enabled_by_default for c in loaded.composites[1:])
    assert set(first.modes) == {"auto", "heat", "off"}


def test_whole_features_can_be_switched_off_on_the_isystem_map():
    full = load_map("diematic_isystem")
    reduced = full.excluding(["solar", "circuit_c", "heat_pump"])
    assert not any(r.group in {"solar", "circuit_c", "heat_pump"} for r in reduced.registers)
    assert "circuit_a_day_temp" in reduced and "circuit_b_day_temp" in reduced
    assert {c.key for c in reduced.composites} == {"circuit_a", "circuit_b"}
    assert len(reduced.registers) < len(full.registers)
    assert reduced.unfiltered is full


def test_every_group_of_the_isystem_map_can_be_named_in_the_exclusion_list():
    full = load_map("diematic_isystem")
    groups = {r.group for r in full.registers if r.group}
    assert {"circuit_a", "circuit_b", "circuit_c", "dhw", "aux", "solar", "cascade"} <= groups
    assert not full.excluding(sorted(groups)).registers


def test_excluding_a_circuit_also_removes_its_weekly_program():
    reduced = load_map("diematic_isystem").excluding(["circuit_c"])
    assert not [r for r in reduced.registers if "circuit_c" in r.key]
    assert "circuit_a_program_monday" in reduced


def test_anticipation_is_in_degree_hours_with_off_above_ten():
    loaded = load_map("diematic_isystem")
    for circuit in ("a", "b", "c"):
        register = loaded.get(f"circuit_{circuit}_anticipation")
        assert register.unit == "°dH"
        assert register.codec.decode([101]) == 10.1
        assert (register.min, register.max, register.step) == (0, 10.1, 0.1)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(0, "P1"), (8192, "P1"), (8193, "P2"), (8194, "P3"), (8195, "P4"), (3, "P4")],
)
@pytest.mark.parametrize("circuit", ["a", "b", "c"])
def test_the_active_program_ignores_the_high_flag_bit(circuit, raw, expected):
    register = load_map("diematic_isystem").get(f"circuit_{circuit}_active_program")
    assert register.codec.decode([raw]) == expected


@pytest.mark.parametrize(
    ("raw", "english"),
    [(0, "French"), (1, "German"), (2, "English"), (6, "Russian"), (7, "Polish"), (8, "Turkish")],
)
def test_regulator_language_numbers_match_a_real_boiler(raw, english):
    assert load_map("diematic_isystem").get("language").codec.decode([raw]) == english


CLOCK_KEYS = (
    "clock_hour",
    "clock_minute",
    "clock_weekday",
    "clock_day",
    "clock_month",
    "clock_year",
)


@pytest.mark.parametrize("name", SHIPPED_MAPS)
def test_both_maps_declare_the_boiler_time_from_their_clock_registers(name):
    loaded = load_map(name)
    stamp = loaded.timestamps[0]
    assert stamp.key == "boiler_time"
    assert set(stamp.parts) == {
        "clock_year",
        "clock_month",
        "clock_day",
        "clock_hour",
        "clock_minute",
    }
    assert all(part in loaded for part in stamp.parts)


@pytest.mark.parametrize("name", SHIPPED_MAPS)
def test_the_clock_is_polled_often_enough_for_a_useful_time(name):
    loaded = load_map(name)
    assert {loaded.get(key).poll_group for key in CLOCK_KEYS} == {"fast"}


@pytest.mark.parametrize("name", SHIPPED_MAPS)
def test_the_boiler_time_is_composed_from_the_real_registers_in_the_configured_zone(name):
    from zoneinfo import ZoneInfo

    from diematic.core.state import StateStore

    loaded = load_map(name)
    store = StateStore(loaded, ZoneInfo("Europe/Warsaw"))
    words = {
        "clock_hour": 10,
        "clock_minute": 30,
        "clock_weekday": 3,
        "clock_day": 15,
        "clock_month": 7,
        "clock_year": 26,
    }
    for key, word in words.items():
        store.ingest(loaded.get(key), [word])
    assert store.value("boiler_time") == "2026-07-15T10:30:00+02:00"


def test_the_isystem_clock_registers_are_read_in_one_block():
    from diematic.registers.planner import plan_blocks

    loaded = load_map("diematic_isystem")
    fast = [r for r in loaded.registers if r.poll_group == "fast"]
    blocks = plan_blocks(fast, loaded.max_block, loaded.max_gap)
    assert any((b.start, b.count) == (679, 6) for b in blocks)


def test_leaving_out_the_system_group_also_drops_the_boiler_time():
    assert load_map("diematic_isystem").excluding(["system"]).timestamps == ()


@pytest.mark.parametrize("key", ["dhw_day_temp", "dhw_night_temp"])
def test_the_dhw_setpoints_go_down_to_the_boilers_own_minimum(key):
    register = load_map("diematic_isystem").get(key)
    assert (register.min, register.max, register.step) == (10, 65, 1)
    assert register.codec.encode(10) == [100]
    assert register.codec.decode([100]) == 10.0
