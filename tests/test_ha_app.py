"""Keeps the Home Assistant app definition in step with the settings it feeds."""

import re
from pathlib import Path

import yaml

from diematic.config.settings import GatewaySettings
from diematic.plugins.mqtt.settings import MqttSettings

APP = Path(__file__).resolve().parent.parent / "diematic_modbus"
CONFIG = yaml.safe_load((APP / "config.yaml").read_text(encoding="utf-8"))
RUN_SH = (APP / "run.sh").read_text(encoding="utf-8")


def exported(prefix):
    return set(re.findall(rf"export ({prefix}_[A-Z_]+)=", RUN_SH))


def known_variables(model, prefix):
    return {f"{prefix}_{name.upper()}" for name in model.model_fields}


def test_every_exported_gateway_variable_is_a_real_setting():
    assert exported("DIEMATIC") <= known_variables(GatewaySettings, "DIEMATIC")


def test_every_exported_mqtt_variable_is_a_real_setting():
    assert exported("MQTT") <= known_variables(MqttSettings, "MQTT")


def test_every_option_in_the_schema_is_read_by_the_script():
    for option in CONFIG["schema"]:
        assert f"'{option}'" in RUN_SH, option


def test_every_option_the_script_reads_exists_in_the_schema():
    read = set(re.findall(r"bashio::config(?:\.has_value)? '([a-z_]+)'", RUN_SH))
    assert read <= set(CONFIG["schema"])


def test_defaults_only_cover_options_that_have_a_schema():
    assert set(CONFIG["options"]) <= set(CONFIG["schema"])


def test_options_without_a_default_are_optional_in_the_schema():
    missing = set(CONFIG["schema"]) - set(CONFIG["options"])
    assert all(CONFIG["schema"][name].endswith("?") for name in missing)


def test_translations_cover_every_option():
    for language in ("en", "pl"):
        translated = yaml.safe_load((APP / "translations" / f"{language}.yaml").read_text("utf-8"))
        assert set(translated["configuration"]) == set(CONFIG["schema"]), language
        for entry in translated["configuration"].values():
            assert entry["name"] and entry["description"]


def test_app_declares_the_fields_the_supervisor_requires():
    for field in ("name", "version", "slug", "description", "arch"):
        assert CONFIG[field]
    assert set(CONFIG["arch"]) <= {"aarch64", "amd64"}


def test_app_waits_for_s6_instead_of_using_docker_init():
    assert CONFIG["init"] is False


def test_script_never_prints_secrets():
    for line in RUN_SH.splitlines():
        if line.lstrip().startswith(("bashio::log", "echo")):
            assert "PASSWORD" not in line.upper()


def test_built_in_maps_offered_in_the_docs_exist():
    from diematic.registers.loader import load_map

    for name in ("diematic_isystem", "diematic3_legacy"):
        assert load_map(name)
    assert CONFIG["options"]["map"] == "diematic_isystem"


def test_the_app_leaves_the_time_zone_to_the_tz_the_supervisor_passes():
    assert "DIEMATIC_TIMEZONE" not in RUN_SH
    assert "bashio::supervisor.timezone" not in RUN_SH
    assert "TZ" in RUN_SH
