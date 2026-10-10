"""MQTT plugin: publishes values, accepts commands and announces entities to Home Assistant."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

import aiomqtt
from pydantic import ValidationError

from diematic.config.settings import format_validation_error
from diematic.core.events import (
    AvailabilityChanged,
    SetRequest,
    SetResult,
    ValueChanged,
)
from diematic.plugins.base import Plugin, PluginContext, PluginError
from diematic.plugins.mqtt.discovery import OFF, ON, DiscoveryBuilder, DiscoveryMessage
from diematic.plugins.mqtt.settings import MqttSettings
from diematic.plugins.mqtt.topics import OFFLINE, ONLINE, Command, Topics
from diematic.registers.model import ClimateDef, RegisterDef

logger = logging.getLogger(__name__)

MAX_PAYLOAD_BYTES = 1024
_TRUE = frozenset({"on", "1", "true", "yes"})
_FALSE = frozenset({"off", "0", "false", "no"})


class IncomingMessage(Protocol):
    @property
    def topic(self) -> Any: ...

    @property
    def payload(self) -> Any: ...


class MqttClient(Protocol):
    async def publish(
        self, topic: str, payload: str | None = None, qos: int = 0, retain: bool = False
    ) -> None: ...

    async def subscribe(self, topic: str, qos: int = 0) -> Any: ...

    @property
    def messages(self) -> AsyncIterator[IncomingMessage]: ...


ClientFactory = Callable[[MqttSettings, Topics], AbstractAsyncContextManager[MqttClient]]


def aiomqtt_client(settings: MqttSettings, topics: Topics) -> AbstractAsyncContextManager[Any]:
    tls = None
    if settings.tls:
        ca = str(settings.tls_ca) if settings.tls_ca else None
        tls = aiomqtt.TLSParameters(ca_certs=ca)
    password = settings.password.get_secret_value() if settings.password else None
    return aiomqtt.Client(
        hostname=settings.host,
        port=settings.port,
        username=settings.username,
        password=password,
        identifier=settings.client_id,
        keepalive=settings.keepalive,
        will=aiomqtt.Will(topics.status, OFFLINE, qos=1, retain=True),
        tls_params=tls,
        tls_insecure=settings.tls_insecure or None,
    )


def format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return ON if value else OFF
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".") or "0"
    return str(value)


def parse_flag(text: str) -> bool | None:
    lowered = text.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    return None


def climate_state(climate: ClimateDef, flags: dict[str, bool]) -> tuple[str | None, str]:
    """Derive the thermostat's mode and preset from the flags of its mode register."""
    active = {name for name, on in flags.items() if on and name in climate.exclusive}
    mode = next((m for m in ("auto", "off", "heat") if climate.modes.get(m) in active), None)
    preset = next((name for name, flag in climate.presets.items() if flag in active), "none")
    if mode is None and preset != "none":
        mode = "heat"
    return mode, preset


class MqttPlugin(Plugin):
    def __init__(
        self, context: PluginContext, client_factory: ClientFactory = aiomqtt_client
    ) -> None:
        super().__init__(context)
        try:
            self.settings = MqttSettings(**context.config)
        except ValidationError as exc:
            raise PluginError(format_validation_error(exc, "mqtt.")) from exc
        self._client_factory = client_factory
        self._topics = Topics(self.settings.topic_prefix)
        self._regmap = context.regmap
        self._climates = {c.key: c for c in self._regmap.composites}
        self._mode_climates = {c.mode_register: c for c in self._regmap.composites}
        builder = DiscoveryBuilder(self._regmap, self.settings, self._topics)
        self._discovery: list[DiscoveryMessage] = builder.build() if self.settings.discovery else []
        self._removals = builder.removals()
        self._subscription = context.events.subscribe(ValueChanged, AvailabilityChanged, SetResult)
        self._birth_topic = f"{self.settings.discovery_prefix}/status"

    async def run(self) -> None:
        async with self._client_factory(self.settings, self._topics) as client:
            try:
                await self._session(client)
            finally:
                with contextlib.suppress(Exception):
                    await client.publish(self._topics.status, OFFLINE, qos=1, retain=True)

    async def _session(self, client: MqttClient) -> None:
        qos = self.settings.qos
        for topic_filter in [*self._topics.command_filters, self._birth_topic]:
            await client.subscribe(topic_filter, qos=qos)
        await client.publish(self._topics.status, ONLINE, qos=1, retain=True)
        await self._announce(client)
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self._read_messages(client))
                group.create_task(self._write_events(client))
        except ExceptionGroup as exc_group:
            raise exc_group.exceptions[0] from None

    async def _announce(self, client: MqttClient) -> None:
        """Publish discovery, retire left-out entities, then availability and current values."""
        for message in self._discovery:
            await client.publish(
                message.topic, json.dumps(message.payload), qos=self.settings.qos, retain=True
            )
        for topic in self._removals:
            await client.publish(topic, None, qos=self.settings.qos, retain=True)
        await self._publish_availability(client, self.context.state.online)
        for event in self.context.state.snapshot():
            await self._publish_value(client, event)

    async def _publish_availability(self, client: MqttClient, online: bool | None) -> None:
        await client.publish(
            self._topics.boiler_status,
            ONLINE if online else OFFLINE,
            qos=self.settings.qos,
            retain=True,
        )

    # --- outgoing -------------------------------------------------------------------------

    async def _write_events(self, client: MqttClient) -> None:
        while True:
            event = await self._subscription.get()
            if isinstance(event, ValueChanged):
                await self._publish_value(client, event)
            elif isinstance(event, AvailabilityChanged):
                await self._publish_availability(client, event.online)
            elif isinstance(event, SetResult):
                result = {"key": event.key, "ok": event.ok, "message": event.message}
                await client.publish(
                    self._topics.set_result, json.dumps(result), qos=self.settings.qos
                )

    async def _publish_value(self, client: MqttClient, event: ValueChanged) -> None:
        qos = self.settings.qos
        register = self._regmap.get(event.key) if event.key in self._regmap else None
        if register is not None and register.type == "bits" and isinstance(event.value, dict):
            for flag, state in event.value.items():
                await client.publish(
                    self._topics.flag_state(event.key, flag),
                    format_value(state),
                    qos=qos,
                    retain=True,
                )
            climate = self._mode_climates.get(event.key)
            if climate is not None:
                await self._publish_climate_state(client, climate, event.value)
            return
        await client.publish(
            self._topics.state(event.key), format_value(event.value), qos=qos, retain=True
        )

    async def _publish_climate_state(
        self, client: MqttClient, climate: ClimateDef, flags: dict[str, bool]
    ) -> None:
        mode, preset = climate_state(climate, flags)
        qos = self.settings.qos
        if mode is not None:
            await client.publish(
                self._topics.climate_mode_state(climate.key), mode, qos=qos, retain=True
            )
        if climate.presets:
            await client.publish(
                self._topics.climate_preset_state(climate.key), preset, qos=qos, retain=True
            )

    # --- incoming -------------------------------------------------------------------------

    async def _read_messages(self, client: MqttClient) -> None:
        async for message in client.messages:
            try:
                await self._handle(client, str(message.topic), message.payload)
            except Exception:
                logger.exception("could not handle a message on %s", message.topic)

    async def _handle(self, client: MqttClient, topic: str, payload: Any) -> None:
        if topic == self._birth_topic:
            if self._text(payload) == ONLINE:
                logger.info("Home Assistant restarted, announcing entities again")
                await self._announce(client)
            return
        command = self._topics.parse_command(topic)
        if command is None:
            return
        text = self._text(payload)
        if text is None:
            logger.warning("ignoring a malformed payload on %s", topic)
            return
        request = self._request_for(command, text)
        if request is not None:
            self.context.events.publish(request)

    @staticmethod
    def _text(payload: Any) -> str | None:
        if isinstance(payload, str):
            raw = payload.encode()
        elif isinstance(payload, bytes | bytearray):
            raw = bytes(payload)
        else:
            return None
        if len(raw) > MAX_PAYLOAD_BYTES:
            return None
        try:
            return raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            return None

    def _request_for(self, command: Command, text: str) -> SetRequest | None:
        match command.kind:
            case "register":
                return self._register_request(command.key, text)
            case "flag":
                state = parse_flag(text)
                if command.flag is None or state is None:
                    logger.warning("ignoring flag command for %s: expected ON or OFF", command.key)
                    return None
                return SetRequest(command.key, bits={command.flag: state})
            case "climate_mode" | "climate_preset":
                return self._climate_request(command, text)
        return None

    def _register_request(self, key: str, text: str) -> SetRequest | None:
        register: RegisterDef | None = self._regmap.get(key) if key in self._regmap else None
        if register is None:
            logger.warning("ignoring command for unknown register %r", key)
            return None
        if register.type in {"temp", "uint"}:
            try:
                number = float(text.replace(",", "."))
            except ValueError:
                logger.warning("ignoring non-numeric payload for %s", key)
                return None
            if not math.isfinite(number):
                return None
            return SetRequest(key, number)
        if register.type == "bits":
            try:
                flags = json.loads(text)
            except ValueError:
                logger.warning("ignoring payload for %s: expected a JSON object of flags", key)
                return None
            if not isinstance(flags, dict) or not all(isinstance(v, bool) for v in flags.values()):
                return None
            return SetRequest(key, bits=flags)
        return SetRequest(key, text)

    def _climate_request(self, command: Command, text: str) -> SetRequest | None:
        climate = self._climates.get(command.key)
        if climate is None:
            logger.warning("ignoring command for unknown thermostat %r", command.key)
            return None
        mapping: dict[str, str] = (
            {mode: flag for mode, flag in climate.modes.items()}
            if command.kind == "climate_mode"
            else climate.presets
        )
        flag = mapping.get(text)
        if flag is None:
            logger.warning("ignoring unsupported %s %r for %s", command.kind[8:], text, climate.key)
            return None
        bits = dict.fromkeys(climate.exclusive, False)
        bits[flag] = True
        return SetRequest(climate.mode_register, bits=bits)
