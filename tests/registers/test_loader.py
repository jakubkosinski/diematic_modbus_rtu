import copy

import pytest

from diematic.registers.loader import MapError, load_map, parse_map

RAW = {
    "system": "test_system",
    "name": "Test system",
    "slave_address": 10,
    "registers": [
        {
            "key": "outdoor_temp",
            "address": 601,
            "type": "temp",
            "kind": "measure",
            "name": {"pl": "Temperatura zewnętrzna", "en": "Outdoor temperature"},
            "unit": "°C",
        },
        {
            "key": "day_setpoint",
            "address": 650,
            "type": "temp",
            "kind": "parameter",
            "name": {"en": "Day setpoint"},
            "min": 5,
            "max": 30,
            "step": 0.5,
        },
        {
            "key": "mode",
            "address": 637,
            "type": "enum",
            "kind": "measure",
            "name": "Mode",
            "values": {0: {"pl": "mróz", "en": "frost"}, 4: {"en": "day"}},
        },
        {
            "key": "forcing",
            "address": 653,
            "type": "bits",
            "kind": "parameter",
            "name": "Forcing",
            "bits": {"auto": {"bit": 3, "name": "Auto", "writable": True}},
        },
        {"key": "units", "address": 507, "type": "uint", "kind": "measure", "name": "Units"},
        {"key": "tens", "address": 508, "type": "uint", "kind": "measure", "name": "Tens"},
    ],
    "time_programs": [{"key": "heating_program", "name": "Heating", "address": 126}],
    "derived": [
        {
            "key": "starts",
            "name": "Starts",
            "terms": [{"key": "tens", "factor": 10}, {"key": "units"}],
        }
    ],
}


def raw_with(**changes):
    data = copy.deepcopy(RAW)
    data.update(changes)
    return data


def test_language_is_resolved_with_english_fallback():
    polish = parse_map(RAW, "pl")
    assert polish.get("outdoor_temp").name == "Temperatura zewnętrzna"
    assert polish.get("day_setpoint").name == "Day setpoint"
    assert polish.get("mode").values == {0: "mróz", 4: "day"}
    english = parse_map(RAW, "en")
    assert english.get("outdoor_temp").name == "Outdoor temperature"


def test_time_programs_expand_to_seven_days_of_three_registers():
    days = [r for r in parse_map(RAW, "pl").registers if r.type == "time_program"]
    assert [r.address for r in days] == [126, 129, 132, 135, 138, 141, 144]
    assert days[0].key == "heating_program_monday"
    assert days[0].name == "Heating - Poniedziałek"
    assert all(r.is_writable and r.poll_group == "slow" for r in days)


def test_registers_are_sorted_by_address():
    addresses = [r.address for r in parse_map(RAW).registers]
    assert addresses == sorted(addresses)


def test_defaults_derive_from_kind():
    parsed = parse_map(RAW)
    assert not parsed.get("outdoor_temp").is_writable
    assert parsed.get("outdoor_temp").poll_group == "fast"
    assert parsed.get("day_setpoint").is_writable
    assert parsed.get("day_setpoint").poll_group == "slow"


def test_codec_follows_type():
    parsed = parse_map(RAW)
    assert parsed.get("outdoor_temp").codec.decode([0x8005]) == -0.5
    assert parsed.get("mode").codec.decode([4]) == "day"
    assert parsed.get("forcing").codec.decode([8]) == {"auto": True}


def test_duplicate_key_rejected():
    data = raw_with(derived=[{"key": "units", "name": "x", "terms": [{"key": "tens"}]}])
    with pytest.raises(MapError, match="duplicate key"):
        parse_map(data)


def test_overlapping_addresses_rejected():
    data = copy.deepcopy(RAW)
    data["registers"].append(
        {"key": "clash", "address": 127, "type": "uint", "kind": "measure", "name": "x"}
    )
    with pytest.raises(MapError, match="address 127"):
        parse_map(data)


def test_derived_unknown_register_rejected():
    data = raw_with(derived=[{"key": "bad", "name": "x", "terms": [{"key": "nope"}]}])
    with pytest.raises(MapError, match="unknown register"):
        parse_map(data)


@pytest.mark.parametrize(
    "register",
    [
        {"key": "Bad-Key", "address": 1, "type": "uint", "kind": "measure", "name": "x"},
        {"key": "a", "address": 1, "type": "enum", "kind": "measure", "name": "x"},
        {
            "key": "a",
            "address": 1,
            "type": "uint",
            "kind": "measure",
            "name": "x",
            "writable": True,
        },
        {
            "key": "a",
            "address": 1,
            "type": "uint",
            "kind": "parameter",
            "name": "x",
            "min": 5,
            "max": 1,
        },
        {"key": "a", "address": 1, "type": "uint", "kind": "measure", "name": "x", "typo": 1},
        {"key": "a", "address": 70000, "type": "uint", "kind": "measure", "name": "x"},
        {
            "key": "a",
            "address": 1,
            "type": "bits",
            "kind": "measure",
            "name": "x",
            "bits": {"p": {"bit": 1, "name": "p"}, "q": {"bit": 1, "name": "q"}},
        },
    ],
)
def test_invalid_register_definitions_rejected(register):
    with pytest.raises(MapError):
        parse_map(raw_with(registers=[register], time_programs=[], derived=[]))


def test_missing_translation_without_english_rejected():
    register = {"key": "a", "address": 1, "type": "uint", "kind": "measure", "name": {"pl": "x"}}
    with pytest.raises(MapError):
        parse_map(raw_with(registers=[register], time_programs=[], derived=[]))


def test_load_map_unknown_name():
    with pytest.raises(MapError, match="not found"):
        load_map("does_not_exist")


def test_load_map_from_file(tmp_path):
    path = tmp_path / "custom.yaml"
    path.write_text(
        "system: custom\nname: Custom\nregisters:\n"
        "  - {key: a, address: 1, type: uint, kind: measure, name: A}\n",
        encoding="utf-8",
    )
    loaded = load_map(path)
    assert loaded.system == "custom" and loaded.get("a").address == 1


def test_load_map_rejects_broken_yaml(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("system: [unclosed", encoding="utf-8")
    with pytest.raises(MapError, match="invalid YAML"):
        load_map(path)


def test_yaml_templates_can_be_merged_into_registers():
    parsed = parse_map(
        __import__("yaml").safe_load(
            """
system: tpl
name: Template
templates:
  temp_measure: &temp_measure {type: temp, kind: measure, unit: "°C", device_class: temperature}
registers:
  - {<<: *temp_measure, key: a, address: 1, name: A}
  - {<<: *temp_measure, key: b, address: 2, name: B, enabled_by_default: false}
"""
        )
    )
    assert parsed.get("a").unit == "°C" and parsed.get("a").enabled_by_default
    assert not parsed.get("b").enabled_by_default


def test_block_limits_default_and_override():
    assert (parse_map(RAW).max_block, parse_map(RAW).max_gap) == (40, 0)
    custom = parse_map(raw_with(max_block=64, max_gap=8))
    assert (custom.max_block, custom.max_gap) == (64, 8)


@pytest.mark.parametrize("limits", [{"max_block": 0}, {"max_block": 126}, {"max_gap": -1}])
def test_invalid_block_limits_rejected(limits):
    with pytest.raises(MapError):
        parse_map(raw_with(**limits))
