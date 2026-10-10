import pytest

from diematic.config.settings import ConfigError, GatewaySettings, load_settings
from diematic.modbus.bus import BusTiming

ENV_VARS = [
    "DIEMATIC_HOST",
    "DIEMATIC_PORT",
    "DIEMATIC_LANGUAGE",
    "DIEMATIC_LOG_LEVEL",
    "DIEMATIC_CONFIG",
    "DIEMATIC_ENABLED_PLUGINS",
    "DIEMATIC_BUS__WINDOW",
]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def write(tmp_path, text, name="config.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_values_come_from_the_yaml_file(tmp_path):
    path = write(tmp_path, "gateway:\n  host: 10.0.0.5\n  port: 20108\n  language: en\n")
    settings = load_settings(path)
    assert (settings.gateway.host, settings.gateway.port, settings.gateway.language) == (
        "10.0.0.5",
        20108,
        "en",
    )


def test_defaults_apply_to_unset_values(tmp_path):
    gateway = load_settings(write(tmp_path, "gateway: {host: h, port: 1}")).gateway
    assert gateway.map == "diematic_isystem"
    assert gateway.language == "pl"
    assert gateway.read_only is False and gateway.trailing_zero is True
    assert gateway.fast_interval == 30 and gateway.slow_interval == 300


def test_environment_overrides_the_file(tmp_path, monkeypatch):
    path = write(tmp_path, "gateway: {host: from-file, port: 1, language: pl}")
    monkeypatch.setenv("DIEMATIC_HOST", "from-env")
    monkeypatch.setenv("DIEMATIC_LANGUAGE", "en")
    gateway = load_settings(path).gateway
    assert (gateway.host, gateway.port, gateway.language) == ("from-env", 1, "en")


def test_environment_alone_is_enough(monkeypatch):
    monkeypatch.setenv("DIEMATIC_HOST", "converter.local")
    monkeypatch.setenv("DIEMATIC_PORT", "502")
    assert load_settings().gateway.port == 502


def test_config_path_can_come_from_the_environment(tmp_path, monkeypatch):
    path = write(tmp_path, "gateway: {host: h, port: 7}")
    monkeypatch.setenv("DIEMATIC_CONFIG", str(path))
    assert load_settings().gateway.port == 7


def test_missing_host_is_reported_by_field_name():
    with pytest.raises(ConfigError, match=r"gateway\.host"):
        load_settings()


def test_misspelled_keys_are_rejected(tmp_path):
    with pytest.raises(ConfigError, match="hots"):
        load_settings(write(tmp_path, "gateway: {hots: h, host: h, port: 1}"))


def test_errors_never_echo_the_offending_value(tmp_path):
    with pytest.raises(ConfigError) as info:
        load_settings(write(tmp_path, "gateway: {host: h, port: 1, language: s3cret-value}"))
    assert "s3cret-value" not in str(info.value) and "language" in str(info.value)


@pytest.mark.parametrize(
    "extra",
    [
        "port: 0",
        "port: 70000",
        "slave_address: 300",
        "timezone: Mars/Olympus",
        "fast_interval: 0",
    ],
)
def test_out_of_range_values_are_rejected(tmp_path, extra):
    text = "gateway: {host: h, port: 1, " + extra + "}"
    with pytest.raises(ConfigError):
        load_settings(write(tmp_path, text))


def test_log_level_is_case_insensitive(tmp_path):
    path = write(tmp_path, "gateway: {host: h, port: 1, log_level: debug}")
    assert load_settings(path).gateway.log_level == "DEBUG"


def test_bus_overrides_are_applied_over_the_default_timing(tmp_path):
    path = write(tmp_path, "gateway: {host: h, port: 1, bus: {window: 3.0}}")
    timing = load_settings(path).gateway.bus.timing()
    assert timing.window == 3.0
    assert timing.master_phase == BusTiming().master_phase


def test_bus_overrides_can_come_from_nested_environment_variables(monkeypatch):
    monkeypatch.setenv("DIEMATIC_HOST", "h")
    monkeypatch.setenv("DIEMATIC_PORT", "1")
    monkeypatch.setenv("DIEMATIC_BUS__WINDOW", "2.5")
    assert load_settings().gateway.bus.timing().window == 2.5


def test_unknown_top_level_section_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown configuration sections"):
        load_settings(write(tmp_path, "gateway: {host: h, port: 1}\nmqtt: {}"))


@pytest.mark.parametrize("text", ["gateway: [unclosed", "- just\n- a list", "gateway: 5"])
def test_malformed_files_are_rejected(tmp_path, text):
    with pytest.raises(ConfigError):
        load_settings(write(tmp_path, text))


def test_unreadable_file_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_settings(tmp_path / "missing.yaml")


def test_mqtt_is_the_default_plugin(tmp_path):
    settings = load_settings(write(tmp_path, "gateway: {host: h, port: 1}"))
    assert settings.enabled_plugins == ["mqtt"] and settings.plugin_config == {}


def test_plugins_listed_in_the_file_are_enabled_with_their_config(tmp_path):
    path = write(
        tmp_path, "gateway: {host: h, port: 1}\nplugins:\n  mqtt: {host: broker}\n  other:\n"
    )
    settings = load_settings(path)
    assert settings.enabled_plugins == ["mqtt", "other"]
    assert settings.plugin_config == {"mqtt": {"host": "broker"}, "other": {}}


def test_enabled_plugins_can_be_overridden_by_the_environment(tmp_path, monkeypatch):
    path = write(tmp_path, "gateway: {host: h, port: 1}\nplugins:\n  mqtt: {}\n")
    monkeypatch.setenv("DIEMATIC_ENABLED_PLUGINS", " a, b ,,")
    assert load_settings(path).enabled_plugins == ["a", "b"]
    monkeypatch.setenv("DIEMATIC_ENABLED_PLUGINS", "")
    assert load_settings(path).enabled_plugins == []


def test_masked_dump_is_json_friendly(tmp_path):
    settings = load_settings(write(tmp_path, "gateway: {host: h, port: 1}"))
    dump = settings.masked()
    assert dump["host"] == "h" and dump["bus"]["window"] is None


def test_gateway_settings_can_be_built_directly(monkeypatch):
    monkeypatch.setenv("DIEMATIC_HOST", "h")
    assert GatewaySettings(port=5).port == 5
