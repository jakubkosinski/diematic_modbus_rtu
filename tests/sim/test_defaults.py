import pytest

from diematic.registers.loader import load_map
from diematic.sim.defaults import default_registers


@pytest.mark.parametrize("name", ["diematic_isystem", "diematic3_legacy"])
def test_every_mapped_word_gets_a_value(name):
    regmap = load_map(name)
    memory = default_registers(regmap)
    for register in regmap.registers:
        assert all(register.address + i in memory for i in range(register.width))


def test_values_decode_to_plausible_numbers():
    regmap = load_map("diematic_isystem")
    memory = default_registers(regmap)

    def decoded(key):
        register = regmap.get(key)
        return register.codec.decode([memory[register.address + i] for i in range(register.width)])

    assert decoded("circuit_a_day_temp") == 17.5
    assert decoded("outdoor_temp") == 21.0
    assert decoded("circuit_a_program_monday") == "07:00-20:00"
    assert decoded("circuit_a_forcing")["auto"] is False
    assert decoded("boiler_type") is not None
    assert decoded("water_pressure") == 0
