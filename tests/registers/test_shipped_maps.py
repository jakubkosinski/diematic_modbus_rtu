import pytest

from diematic.registers.loader import load_map

SHIPPED_MAPS = ["diematic_isystem"]


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
