import pytest
from pydantic import ValidationError

from diematic.plugins.mqtt.settings import MqttSettings
from diematic.plugins.mqtt.topics import Command, Topics

ENV_VARS = [
    "MQTT_HOST",
    "MQTT_PORT",
    "MQTT_USERNAME",
    "MQTT_PASSWORD",
    "MQTT_TOPIC_PREFIX",
    "MQTT_TLS",
    "MQTT_QOS",
]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class TestSettings:
    def test_defaults(self):
        settings = MqttSettings()
        assert (settings.host, settings.port, settings.qos) == ("localhost", 1883, 1)
        assert settings.discovery is True and settings.discovery_prefix == "homeassistant"
        assert settings.topic_prefix == "diematic" and settings.password is None

    def test_environment_overrides_values_from_the_config_file(self, monkeypatch):
        monkeypatch.setenv("MQTT_HOST", "from-env")
        monkeypatch.setenv("MQTT_PORT", "8883")
        monkeypatch.setenv("MQTT_TLS", "true")
        settings = MqttSettings(host="from-file", port=1883, username="file-user")
        assert (settings.host, settings.port, settings.tls) == ("from-env", 8883, True)
        assert settings.username == "file-user"

    def test_password_is_masked_everywhere(self, monkeypatch):
        monkeypatch.setenv("MQTT_PASSWORD", "hunter2-secret")
        settings = MqttSettings()
        assert settings.password.get_secret_value() == "hunter2-secret"
        assert "hunter2-secret" not in repr(settings)
        assert "hunter2-secret" not in str(settings)
        assert "hunter2-secret" not in str(settings.model_dump(mode="json"))

    def test_topic_prefix_is_normalised(self):
        assert MqttSettings(topic_prefix="/home/heater/").topic_prefix == "home/heater"

    @pytest.mark.parametrize("prefix", ["", "/", "a/+/b", "a/#", "a b", "a//b"])
    def test_invalid_topic_prefixes_are_rejected(self, prefix):
        with pytest.raises(ValidationError):
            MqttSettings(topic_prefix=prefix)

    @pytest.mark.parametrize(
        "values",
        [{"port": 0}, {"qos": 3}, {"node_id": "bad id"}, {"keepalive": 1}, {"unknown": 1}],
    )
    def test_invalid_values_are_rejected(self, values):
        with pytest.raises(ValidationError):
            MqttSettings(**values)


class TestTopics:
    topics = Topics("diematic")

    def test_layout(self):
        t = self.topics
        assert t.status == "diematic/status"
        assert t.boiler_status == "diematic/boiler_status"
        assert t.state("outdoor_temp") == "diematic/outdoor_temp"
        assert t.command("outdoor_temp") == "diematic/outdoor_temp/set"
        assert t.flag_state("forcing", "auto") == "diematic/forcing/auto"
        assert t.flag_command("forcing", "auto") == "diematic/forcing/auto/set"
        assert t.climate_mode_state("circuit_a") == "diematic/climate/circuit_a/mode"
        assert t.climate_preset_command("circuit_a") == "diematic/climate/circuit_a/preset/set"

    def test_command_filters_cover_every_command_topic(self):
        assert self.topics.command_filters == [
            "diematic/+/set",
            "diematic/+/+/set",
            "diematic/climate/+/+/set",
        ]

    @pytest.mark.parametrize(
        ("topic", "expected"),
        [
            ("diematic/setpoint/set", Command("register", "setpoint")),
            ("diematic/forcing/auto/set", Command("flag", "forcing", "auto")),
            ("diematic/climate/circuit_a/mode/set", Command("climate_mode", "circuit_a")),
            ("diematic/climate/circuit_a/preset/set", Command("climate_preset", "circuit_a")),
        ],
    )
    def test_parse_command(self, topic, expected):
        assert self.topics.parse_command(topic) == expected

    @pytest.mark.parametrize(
        "topic",
        [
            "other/setpoint/set",
            "diematic/setpoint",
            "diematic//set",
            "diematic/a//set",
            "diematic/climate/circuit_a/set",
            "diematic/climate/circuit_a/mode/extra/set",
            "diematic/a/b/c/set",
            "diematic/set",
        ],
    )
    def test_foreign_or_malformed_topics_are_ignored(self, topic):
        assert self.topics.parse_command(topic) is None

    def test_a_flag_named_set_is_still_a_flag_command(self):
        assert self.topics.parse_command("diematic/status/set/set") == Command(
            "flag", "status", "set"
        )
