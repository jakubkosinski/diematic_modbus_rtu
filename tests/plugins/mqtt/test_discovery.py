import json
import re

import pytest

from diematic.plugins.mqtt.discovery import PROGRAM_PATTERN, DiscoveryBuilder
from diematic.plugins.mqtt.settings import MqttSettings
from diematic.plugins.mqtt.topics import Topics
from diematic.registers.codecs import TimeProgramCodec
from diematic.registers.loader import load_map


def build(name="diematic_isystem", language="en", **settings):
    regmap = load_map(name, language)
    config = MqttSettings(**settings)
    return regmap, DiscoveryBuilder(regmap, config, Topics(config.topic_prefix)).build()


def by_topic(messages):
    return {m.topic: m.payload for m in messages}


def entity(messages, component, object_key, node="diematic", prefix="homeassistant"):
    return by_topic(messages)[f"{prefix}/{component}/{node}/{object_key}/config"]


@pytest.mark.parametrize("language", ["en", "pl"])
@pytest.mark.parametrize("name", ["diematic_isystem", "diematic3_legacy"])
def test_identifiers_are_unique_and_payloads_serialisable(name, language):
    _, messages = build(name, language)
    assert len({m.topic for m in messages}) == len(messages)
    assert len({m.payload["unique_id"] for m in messages}) == len(messages)
    assert len({m.payload["default_entity_id"] for m in messages}) == len(messages)
    for message in messages:
        json.dumps(message.payload)


def test_every_register_is_exposed_by_some_entity():
    regmap, messages = build()
    exposed = {m.payload["unique_id"] for m in messages}
    for register in regmap.registers:
        if register.type == "bits":
            assert any(f"diematic_{register.key}_{flag}" in exposed for flag in register.bits)
        else:
            assert f"diematic_{register.key}" in exposed, register.key


def test_entities_only_depend_on_the_bridge_so_they_keep_their_last_value():
    _, messages = build()
    for message in messages:
        payload = message.payload
        assert payload["device"]["identifiers"] == ["diematic"]
        assert [a["topic"] for a in payload["availability"]] == ["diematic/status"]


def test_entities_can_be_made_to_follow_the_boiler_as_well():
    _, messages = build(boiler_availability=True)
    for message in messages:
        payload = message.payload
        if message.topic.endswith("/boiler_connection/config"):
            continue
        assert payload["availability_mode"] == "all"
        assert [a["topic"] for a in payload["availability"]] == [
            "diematic/status",
            "diematic/boiler_status",
        ]


@pytest.mark.parametrize("boiler_availability", [False, True])
def test_the_boiler_connection_sensor_stays_available_whatever_the_boiler_does(
    boiler_availability,
):
    _, messages = build(boiler_availability=boiler_availability)
    sensor = entity(messages, "binary_sensor", "boiler_connection")
    assert sensor["state_topic"] == "diematic/boiler_status"
    assert (sensor["payload_on"], sensor["payload_off"]) == ("online", "offline")
    assert sensor["device_class"] == "connectivity"
    assert sensor["entity_category"] == "diagnostic"
    assert [a["topic"] for a in sensor["availability"]] == ["diematic/status"]


def test_the_boiler_connection_sensor_is_named_in_the_map_language():
    _, english = build(language="en")
    _, polish = build(language="pl")
    assert entity(english, "binary_sensor", "boiler_connection")["name"] == "Boiler connection"
    assert entity(polish, "binary_sensor", "boiler_connection")["name"] == "Połączenie z kotłem"


def test_measure_becomes_a_sensor_with_units_and_classes():
    _, messages = build()
    sensor = entity(messages, "sensor", "outdoor_temp")
    assert sensor["state_topic"] == "diematic/outdoor_temp"
    assert sensor["unit_of_measurement"] == "°C"
    assert sensor["device_class"] == "temperature"
    assert sensor["state_class"] == "measurement"
    assert "command_topic" not in sensor


def test_enum_measure_becomes_an_enum_sensor_with_its_options():
    _, messages = build()
    sensor = entity(messages, "sensor", "season_mode")
    assert sensor["device_class"] == "enum"
    assert sensor["options"] == ["Summer", "Winter"]
    assert "unit_of_measurement" not in sensor


def test_writable_number_carries_limits_and_command_topic():
    _, messages = build()
    number = entity(messages, "number", "circuit_a_day_temp")
    assert (number["min"], number["max"], number["step"]) == (5, 30, 0.5)
    assert number["command_topic"] == "diematic/circuit_a_day_temp/set"
    assert number["state_topic"] == "diematic/circuit_a_day_temp"
    assert number["unit_of_measurement"] == "°C"


def test_writable_enum_becomes_a_select():
    _, messages = build()
    select = entity(messages, "select", "dhw_priority")
    assert select["options"] == ["Total", "Sliding", "None"]
    assert select["command_topic"] == "diematic/dhw_priority/set"


def test_writable_flags_become_switches_and_read_only_flags_binary_sensors():
    _, messages = build()
    switch = entity(messages, "switch", "circuit_a_forcing_auto")
    assert switch["state_topic"] == "diematic/circuit_a_forcing/auto"
    assert switch["command_topic"] == "diematic/circuit_a_forcing/auto/set"
    assert (switch["payload_on"], switch["payload_off"]) == ("ON", "OFF")
    binary = entity(messages, "binary_sensor", "boiler_states_burner_on")
    assert binary["state_topic"] == "diematic/boiler_states/burner_on"
    assert "command_topic" not in binary


def test_time_program_is_an_enabled_text_entity_with_a_pattern():
    _, messages = build()
    text = entity(messages, "text", "circuit_a_program_monday")
    assert text["command_topic"] == "diematic/circuit_a_program_monday/set"
    assert text["pattern"] == PROGRAM_PATTERN
    assert "enabled_by_default" not in text


@pytest.mark.parametrize("value", ["", "07:00-20:00", "05:30-08:00,16:00-22:30"])
def test_program_pattern_accepts_what_the_codec_produces(value):
    assert re.fullmatch(PROGRAM_PATTERN, value)
    assert re.fullmatch(
        PROGRAM_PATTERN, TimeProgramCodec().decode(TimeProgramCodec().encode(value))
    )


@pytest.mark.parametrize("value", ["7-20", "07:00-20:00,", "abc", "07:00"])
def test_program_pattern_rejects_malformed_text(value):
    assert not re.fullmatch(PROGRAM_PATTERN, value)


def test_helper_registers_are_disabled_by_default():
    _, messages = build()
    assert entity(messages, "sensor", "heating_energy_kwh")["enabled_by_default"] is False
    assert "enabled_by_default" not in entity(messages, "sensor", "outdoor_temp")


def test_derived_sensors_are_published():
    _, messages = build()
    energy = entity(messages, "sensor", "heating_electrical_energy")
    assert energy["device_class"] == "energy" and energy["state_class"] == "total_increasing"


def test_entity_categories_follow_the_map_groups():
    _, messages = build()
    assert entity(messages, "number", "heating_pump_overrun")["entity_category"] == "config"
    assert entity(messages, "sensor", "controller_software_version")["entity_category"] == (
        "diagnostic"
    )
    assert "entity_category" not in entity(messages, "number", "circuit_a_day_temp")


def test_thermostat_is_built_from_existing_register_topics():
    _, messages = build()
    climate = entity(messages, "climate", "circuit_a")
    assert climate["current_temperature_topic"] == "diematic/circuit_a_room_temp"
    assert climate["temperature_low_command_topic"] == "diematic/circuit_a_night_temp/set"
    assert climate["temperature_high_state_topic"] == "diematic/circuit_a_day_temp"
    assert climate["modes"] == ["auto", "heat", "off"]
    assert climate["mode_command_topic"] == "diematic/climate/circuit_a/mode/set"
    assert climate["preset_modes"] == ["night", "day"]
    assert (climate["min_temp"], climate["max_temp"], climate["temp_step"]) == (5, 30, 0.5)
    assert "enabled_by_default" not in climate
    assert entity(messages, "climate", "circuit_b")["enabled_by_default"] is False


def test_prefixes_and_node_id_are_configurable():
    _, messages = build(discovery_prefix="ha", node_id="boiler1", topic_prefix="home/heater")
    sensor = entity(messages, "sensor", "outdoor_temp", node="boiler1", prefix="ha")
    assert sensor["state_topic"] == "home/heater/outdoor_temp"
    assert sensor["unique_id"] == "boiler1_outdoor_temp"
    assert sensor["device"]["identifiers"] == ["boiler1"]


def test_names_follow_the_map_language():
    _, polish = build(language="pl")
    assert entity(polish, "sensor", "outdoor_temp")["name"] == "Temperatura zewnętrzna"
    assert entity(polish, "climate", "circuit_a")["name"] == "Obieg A"


def reduced_builder(names):
    regmap = load_map("diematic_isystem", "en").excluding(names)
    config = MqttSettings()
    return DiscoveryBuilder(regmap, config, Topics(config.topic_prefix))


def test_nothing_is_retired_when_nothing_was_left_out():
    regmap = load_map("diematic_isystem", "en")
    builder = DiscoveryBuilder(regmap, MqttSettings(), Topics("diematic"))
    assert builder.removals() == []


def test_left_out_entities_are_not_announced():
    announced = by_topic(reduced_builder(["circuit_c", "solar"]).build())
    assert "homeassistant/climate/diematic/circuit_c/config" not in announced
    assert "homeassistant/number/diematic/circuit_c_day_temp/config" not in announced
    assert "homeassistant/sensor/diematic/solar_collector_temp/config" not in announced
    assert "homeassistant/climate/diematic/circuit_a/config" in announced


def test_removals_cover_configs_states_flags_and_thermostat_topics_of_what_was_left_out():
    removals = set(reduced_builder(["circuit_c", "solar"]).removals())
    assert "homeassistant/climate/diematic/circuit_c/config" in removals
    assert "homeassistant/number/diematic/circuit_c_day_temp/config" in removals
    assert "homeassistant/switch/diematic/circuit_c_forcing_auto/config" in removals
    assert "homeassistant/text/diematic/circuit_c_program_monday/config" in removals
    assert "diematic/circuit_c_day_temp" in removals
    assert "diematic/circuit_c_forcing/auto" in removals
    assert "diematic/climate/circuit_c/mode" in removals
    assert "diematic/climate/circuit_c/preset" in removals
    assert "diematic/solar_collector_temp" in removals


def test_removals_never_touch_what_is_still_announced():
    builder = reduced_builder(["circuit_c", "solar"])
    removals = set(builder.removals())
    assert not removals & set(by_topic(builder.build()))
    assert "diematic/circuit_a_day_temp" not in removals
    assert "diematic/climate/circuit_a/mode" not in removals


def test_derived_sensors_that_lost_their_registers_are_retired():
    removals = set(reduced_builder(["service"]).removals())
    assert "homeassistant/sensor/diematic/heating_electrical_energy/config" in removals
    assert "diematic/burner_hours" in removals
