"""Home Assistant MQTT discovery messages built from a register map."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from diematic.plugins.mqtt.settings import MqttSettings
from diematic.plugins.mqtt.topics import OFFLINE, ONLINE, Topics
from diematic.registers.loader import RegisterMap
from diematic.registers.model import ClimateDef, DerivedDef, RegisterDef

ON = "ON"
OFF = "OFF"

# Matches what TimeProgramCodec accepts: comma separated HH:MM-HH:MM periods, or nothing.
PROGRAM_PATTERN = r"^(\d{1,2}:\d{2}-\d{1,2}:\d{2}(,\d{1,2}:\d{2}-\d{1,2}:\d{2})*)?$"

_DIAGNOSTIC_GROUPS = frozenset({"service", "solar", "heat_pump", "cascade"})
_CONFIG_GROUPS = frozenset(
    {"installation", "system", "stop_periods", "solar", "cascade", "service"}
)
# Strings arrive as text, so an empty payload means "no value".
_NUMERIC_TEMPLATE = "{{ value if value else none }}"
_CONNECTION_NAME = {"en": "Boiler connection", "pl": "Połączenie z kotłem"}


@dataclass(frozen=True, slots=True)
class DiscoveryMessage:
    topic: str
    payload: dict[str, Any]


class DiscoveryBuilder:
    def __init__(self, regmap: RegisterMap, settings: MqttSettings, topics: Topics) -> None:
        self._regmap = regmap
        self._settings = settings
        self._topics = topics
        self._device = {
            "identifiers": [settings.node_id],
            "name": settings.device_name,
            "manufacturer": "De Dietrich",
            "model": regmap.name,
        }
        bridge = {
            "topic": topics.status,
            "payload_available": ONLINE,
            "payload_not_available": OFFLINE,
        }
        boiler = {
            "topic": topics.boiler_status,
            "payload_available": ONLINE,
            "payload_not_available": OFFLINE,
        }
        # By default entities keep their last value while the boiler is silent; only the bridge
        # going away makes them unavailable. The boiler's own state is a separate sensor.
        self._bridge_only = [bridge]
        self._availability = [bridge, boiler] if settings.boiler_availability else [bridge]

    def build(self) -> list[DiscoveryMessage]:
        messages: list[DiscoveryMessage] = []
        for register in self._regmap.registers:
            messages.extend(self._register(register))
        for derived in self._regmap.derived:
            messages.append(self._derived(derived))
        for climate in self._regmap.composites:
            messages.append(self._climate(climate))
        messages.append(self._connection())
        return messages

    def _connection(self) -> DiscoveryMessage:
        name = _CONNECTION_NAME.get(self._regmap.language, _CONNECTION_NAME["en"])
        message = self._message(
            "binary_sensor",
            "boiler_connection",
            name,
            {
                "state_topic": self._topics.boiler_status,
                "payload_on": ONLINE,
                "payload_off": OFFLINE,
                "device_class": "connectivity",
            },
            category="diagnostic",
        )
        message.payload["availability"] = self._bridge_only
        return message

    def removals(self) -> list[str]:
        """Topics left over from entities the map no longer has.

        Publishing an empty retained message to each deletes the entity from Home Assistant, or
        the value it last published, so nothing lingers as 'unknown' after a register is left out.
        """
        original = self._regmap.unfiltered
        if original is None:
            return []
        topics = self._topics
        kept = {message.topic for message in self.build()}
        stale = [
            message.topic
            for message in DiscoveryBuilder(original, self._settings, topics).build()
            if message.topic not in kept
        ]
        present = {r.key for r in self._regmap.registers} | {d.key for d in self._regmap.derived}
        for register in original.registers:
            if register.key in present:
                continue
            if register.type == "bits":
                stale += [topics.flag_state(register.key, flag) for flag in register.bits or {}]
            else:
                stale.append(topics.state(register.key))
        stale += [topics.state(d.key) for d in original.derived if d.key not in present]
        thermostats = {c.key for c in self._regmap.composites}
        for climate in original.composites:
            if climate.key not in thermostats:
                stale += [
                    topics.climate_mode_state(climate.key),
                    topics.climate_preset_state(climate.key),
                ]
        return stale

    def _message(
        self,
        component: str,
        object_key: str,
        name: str,
        fields: dict[str, Any],
        *,
        enabled: bool = True,
        category: str | None = None,
    ) -> DiscoveryMessage:
        node = self._settings.node_id
        payload: dict[str, Any] = {
            "name": name,
            "unique_id": f"{node}_{object_key}",
            "default_entity_id": f"{component}.{node}_{object_key}",
            "device": self._device,
            "availability": self._availability,
            "availability_mode": "all",
            "qos": self._settings.qos,
            **{k: v for k, v in fields.items() if v is not None},
        }
        if not enabled:
            payload["enabled_by_default"] = False
        if category:
            payload["entity_category"] = category
        topic = f"{self._settings.discovery_prefix}/{component}/{node}/{object_key}/config"
        return DiscoveryMessage(topic, payload)

    @staticmethod
    def _category(register: RegisterDef | DerivedDef) -> str | None:
        kind = getattr(register, "kind", "measure")
        group = register.group
        if kind == "measure" and group in _DIAGNOSTIC_GROUPS:
            return "diagnostic"
        if kind == "parameter" and group in _CONFIG_GROUPS:
            return "config"
        return None

    def _register(self, register: RegisterDef) -> list[DiscoveryMessage]:
        enabled = register.enabled_by_default
        category = self._category(register)
        if register.type == "bits":
            return self._flags(register, enabled, category)
        if register.type == "time_program":
            return [self._program(register, enabled, category)]
        if register.is_writable:
            if register.type == "enum":
                return [self._select(register, enabled, category)]
            return [self._number(register, enabled, category)]
        return [self._sensor(register, enabled, category)]

    def _sensor(
        self, register: RegisterDef, enabled: bool, category: str | None
    ) -> DiscoveryMessage:
        fields: dict[str, Any] = {"state_topic": self._topics.state(register.key)}
        if register.type == "enum":
            fields["device_class"] = "enum"
            fields["options"] = list((register.values or {}).values())
        else:
            fields.update(
                unit_of_measurement=register.unit,
                device_class=register.device_class,
                state_class=register.state_class,
                value_template=_NUMERIC_TEMPLATE,
            )
        fields["icon"] = register.icon
        return self._message(
            "sensor", register.key, register.name, fields, enabled=enabled, category=category
        )

    def _number(
        self, register: RegisterDef, enabled: bool, category: str | None
    ) -> DiscoveryMessage:
        fields = {
            "state_topic": self._topics.state(register.key),
            "command_topic": self._topics.command(register.key),
            "min": register.min,
            "max": register.max,
            "step": register.step,
            "mode": "box",
            "unit_of_measurement": register.unit,
            "device_class": register.device_class,
            "icon": register.icon,
            "value_template": _NUMERIC_TEMPLATE,
        }
        return self._message(
            "number", register.key, register.name, fields, enabled=enabled, category=category
        )

    def _select(
        self, register: RegisterDef, enabled: bool, category: str | None
    ) -> DiscoveryMessage:
        fields = {
            "state_topic": self._topics.state(register.key),
            "command_topic": self._topics.command(register.key),
            "options": list((register.values or {}).values()),
            "icon": register.icon,
        }
        return self._message(
            "select", register.key, register.name, fields, enabled=enabled, category=category
        )

    def _program(
        self, register: RegisterDef, enabled: bool, category: str | None
    ) -> DiscoveryMessage:
        fields: dict[str, Any] = {
            "state_topic": self._topics.state(register.key),
            "icon": register.icon or "mdi:calendar-clock",
        }
        if not register.is_writable:
            return self._message(
                "sensor", register.key, register.name, fields, enabled=enabled, category=category
            )
        fields.update(
            command_topic=self._topics.command(register.key),
            min=0,
            max=255,
            mode="text",
            pattern=PROGRAM_PATTERN,
        )
        return self._message(
            "text", register.key, register.name, fields, enabled=enabled, category=category
        )

    def _flags(
        self, register: RegisterDef, enabled: bool, category: str | None
    ) -> list[DiscoveryMessage]:
        messages = []
        for flag, definition in (register.bits or {}).items():
            object_key = f"{register.key}_{flag}"
            fields: dict[str, Any] = {
                "state_topic": self._topics.flag_state(register.key, flag),
                "payload_on": ON,
                "payload_off": OFF,
            }
            writable = definition.writable and register.is_writable
            if writable:
                fields["command_topic"] = self._topics.flag_command(register.key, flag)
                component = "switch"
            else:
                fields["device_class"] = definition.device_class
                component = "binary_sensor"
            messages.append(
                self._message(
                    component,
                    object_key,
                    f"{register.name}: {definition.name}",
                    fields,
                    enabled=enabled,
                    category=category,
                )
            )
        return messages

    def _derived(self, derived: DerivedDef) -> DiscoveryMessage:
        fields = {
            "state_topic": self._topics.state(derived.key),
            "unit_of_measurement": derived.unit,
            "device_class": derived.device_class,
            "state_class": derived.state_class,
            "value_template": _NUMERIC_TEMPLATE,
        }
        return self._message(
            "sensor",
            derived.key,
            derived.name,
            fields,
            enabled=derived.enabled_by_default,
            category=self._category(derived),
        )

    def _climate(self, climate: ClimateDef) -> DiscoveryMessage:
        topics = self._topics
        low = self._regmap.get(climate.low)
        high = self._regmap.get(climate.high)
        fields: dict[str, Any] = {
            "current_temperature_topic": topics.state(climate.current_temperature),
            "current_temperature_template": _NUMERIC_TEMPLATE,
            "temperature_low_state_topic": topics.state(climate.low),
            "temperature_low_command_topic": topics.command(climate.low),
            "temperature_high_state_topic": topics.state(climate.high),
            "temperature_high_command_topic": topics.command(climate.high),
            "modes": [mode for mode in ("auto", "heat", "off") if mode in climate.modes],
            "mode_state_topic": topics.climate_mode_state(climate.key),
            "mode_command_topic": topics.climate_mode_command(climate.key),
            "min_temp": low.min,
            "max_temp": high.max,
            "temp_step": high.step,
            "temperature_unit": "C",
            "precision": 0.1,
        }
        if climate.presets:
            fields["preset_modes"] = list(climate.presets)
            fields["preset_mode_state_topic"] = topics.climate_preset_state(climate.key)
            fields["preset_mode_command_topic"] = topics.climate_preset_command(climate.key)
        return self._message(
            "climate", climate.key, climate.name, fields, enabled=climate.enabled_by_default
        )
