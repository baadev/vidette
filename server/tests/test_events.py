from __future__ import annotations

import asyncio

from vidette.core.events import Event, InProcessEventBus, topic_matches


def test_topic_matches() -> None:
    assert topic_matches("event.confirmed", "event.confirmed")
    assert topic_matches("event.*", "event.confirmed")
    assert topic_matches("system.*", "system.storage.pressure")
    assert topic_matches("*", "anything")
    assert not topic_matches("event.confirmed", "event.enriched")
    assert not topic_matches("event.*", "system.health")


def test_event_ids_are_time_sortable() -> None:
    first = Event(camera="a")
    second = Event(camera="a")
    assert first.id != second.id
    assert first.id[:12] <= second.id[:12]  # millisecond prefix keeps ids time-ordered


async def test_bus_delivers_by_pattern() -> None:
    bus = InProcessEventBus()
    events_sub = bus.subscribe("event.*")
    system_sub = bus.subscribe("system.*")

    await bus.publish("event.confirmed", {"id": "1"})
    await bus.publish("system.health", {"ok": True})

    topic, payload = await asyncio.wait_for(events_sub.get(), timeout=1)
    assert (topic, payload) == ("event.confirmed", {"id": "1"})
    topic, payload = await asyncio.wait_for(system_sub.get(), timeout=1)
    assert topic == "system.health"

    events_sub.close()
    await bus.publish("event.enriched", {"id": "2"})  # no crash after close


async def test_bus_bounded_queue_drops_oldest_and_counts() -> None:
    bus = InProcessEventBus(max_queue_size=2)
    subscription = bus.subscribe("*")

    for index in range(3):
        await bus.publish("event.confirmed", {"seq": index})

    assert bus.dropped == 1
    _, payload = await asyncio.wait_for(subscription.get(), timeout=1)
    assert payload == {"seq": 1}  # oldest (seq 0) was dropped, fresh events win
