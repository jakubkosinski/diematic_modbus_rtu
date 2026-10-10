import asyncio

from diematic.core.events import (
    AvailabilityChanged,
    EventBus,
    SetRequest,
    SetResult,
    ValueChanged,
)


async def test_subscriber_receives_only_requested_types():
    bus = EventBus()
    values = bus.subscribe(ValueChanged)
    bus.publish(AvailabilityChanged(True))
    bus.publish(ValueChanged("a", 1))
    event = await asyncio.wait_for(values.get(), 1)
    assert event == ValueChanged("a", 1)
    assert values.queue.empty()


async def test_subscribing_without_types_receives_everything():
    bus = EventBus()
    everything = bus.subscribe()
    bus.publish(SetRequest("k", 1))
    bus.publish(SetResult("k", True))
    assert everything.queue.qsize() == 2


async def test_every_subscriber_gets_its_own_copy():
    bus = EventBus()
    first, second = bus.subscribe(ValueChanged), bus.subscribe(ValueChanged)
    bus.publish(ValueChanged("a", 1))
    assert first.queue.qsize() == second.queue.qsize() == 1


async def test_full_queue_drops_the_oldest_event_and_never_blocks():
    bus = EventBus()
    slow = bus.subscribe(ValueChanged, maxsize=2)
    for value in range(5):
        bus.publish(ValueChanged("a", value))
    assert [slow.queue.get_nowait().value for _ in range(2)] == [3, 4]
    assert slow.dropped == 3


async def test_closed_subscription_stops_receiving():
    bus = EventBus()
    subscription = bus.subscribe(ValueChanged)
    subscription.close()
    bus.publish(ValueChanged("a", 1))
    assert subscription.queue.empty()
    subscription.close()
