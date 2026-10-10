"""In-process event bus connecting the core to broker plugins."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ValueChanged:
    key: str
    value: Any
    raw: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class AvailabilityChanged:
    online: bool


@dataclass(frozen=True, slots=True)
class SetRequest:
    """Ask the core to change a register: a whole value, or named flags of a bits register."""

    key: str
    value: Any = None
    bits: dict[str, bool] | None = None


@dataclass(frozen=True, slots=True)
class SetResult:
    key: str
    ok: bool
    message: str = ""


Event = ValueChanged | AvailabilityChanged | SetRequest | SetResult


class Subscription:
    def __init__(self, bus: EventBus, types: tuple[type, ...], maxsize: int) -> None:
        self._bus = bus
        self.types = types
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize)
        self.dropped = 0

    async def get(self) -> Event:
        return await self.queue.get()

    def close(self) -> None:
        self._bus.unsubscribe(self)

    def _offer(self, event: Event) -> None:
        if not isinstance(event, self.types):
            return
        if self.queue.full():
            self.queue.get_nowait()
            self.dropped += 1
        self.queue.put_nowait(event)


@dataclass
class EventBus:
    """Fan-out of events. Publishing never blocks: a full queue drops its oldest event."""

    _subscriptions: list[Subscription] = field(default_factory=list)

    def subscribe(self, *types: type, maxsize: int = 1000) -> Subscription:
        subscription = Subscription(self, types or (object,), maxsize)
        self._subscriptions.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        if subscription in self._subscriptions:
            self._subscriptions.remove(subscription)

    def publish(self, event: Event) -> None:
        for subscription in self._subscriptions:
            subscription._offer(event)
