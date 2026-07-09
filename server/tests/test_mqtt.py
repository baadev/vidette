"""MQTT bridge: HA discovery announce, occupancy state, reconnect with rate-limited failure
events, and clean shutdown — against a fake client, no broker, no wall-clock sleeps."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any, NamedTuple

import aiomqtt

from vidette.core.config import VidetteConfig
from vidette.core.events import InProcessEventBus
from vidette.notify.mqtt import MqttPublisher

CONFIRMED: dict[str, Any] = {
    "event": "event.confirmed",
    "id": "01hxv7q8e9",
    "camera": "front-door",
    "started_at": "2026-07-07T21:14:03.412Z",
    "ended_at": None,
    "kinds": ["person"],
    "zones": ["door"],
    "geometry": {"approach": 0.92, "dwell_s": 14.2, "touch": True, "repeat_pass": 0},
    "summary": None,
    "intent": None,
    "policy": "entry-interest",
    "media": {"snapshot_url": "/api/v1/events/01hxv7q8e9/snapshot.webp"},
}


async def wait_for(condition: Callable[[], object], timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(0.01)


class EmitRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, topic: str, payload: dict[str, Any]) -> None:
        self.calls.append((topic, payload))

    def topics(self, topic: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.calls if name == topic]


class PublishCall(NamedTuple):
    topic: str
    payload: str | bytes | None
    qos: int
    retain: bool


class FakeMqttClient:
    """Async-context-manager fake recording publishes; failures are injectable."""

    def __init__(self, *, connect_error: Exception | None = None) -> None:
        self.connect_error = connect_error
        self.publish_errors: dict[str, Exception] = {}  # topic → raised once, then removed
        self.publishes: list[PublishCall] = []
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> FakeMqttClient:
        if self.connect_error is not None:
            raise self.connect_error
        self.entered = True
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.exited = True

    async def publish(
        self, topic: str, payload: str | bytes | None = None, qos: int = 0, retain: bool = False
    ) -> None:
        error = self.publish_errors.pop(topic, None)
        if error is not None:
            raise error
        self.publishes.append(PublishCall(topic, payload, qos, retain))

    def topics(self, topic: str) -> list[PublishCall]:
        return [call for call in self.publishes if call.topic == topic]


class FakeClientFactory:
    """One fresh fake client per connection attempt; early attempts can fail to connect."""

    def __init__(self, connect_errors: list[Exception] | None = None) -> None:
        self._connect_errors = list(connect_errors or [])
        self.clients: list[FakeMqttClient] = []

    def __call__(self) -> FakeMqttClient:
        error = self._connect_errors.pop(0) if self._connect_errors else None
        client = FakeMqttClient(connect_error=error)
        self.clients.append(client)
        return client

    @property
    def current(self) -> FakeMqttClient:
        return self.clients[-1]


def as_text(call: PublishCall) -> str:
    assert isinstance(call.payload, str)
    return call.payload


def mqtt_config(*, enabled: bool = True, discovery: bool = True) -> VidetteConfig:
    return VidetteConfig.model_validate(
        {
            "cameras": {
                "front-door": {
                    "name": "Front door",
                    "source": {"main": "rtsp://user:pw@203.0.113.10:554/stream1"},
                },
                "backyard": {"source": {"main": "rtsp://user:pw@203.0.113.11:554/stream1"}},
            },
            "integrations": {
                "mqtt": {
                    "enabled": enabled,
                    "host": "broker.local" if enabled else None,
                    "discovery": discovery,
                }
            },
        }
    )


def make_publisher(
    config: VidetteConfig, *, factory: FakeClientFactory | None = None
) -> tuple[MqttPublisher, InProcessEventBus, FakeClientFactory, EmitRecorder]:
    bus = InProcessEventBus()
    emit = EmitRecorder()
    resolved = factory if factory is not None else FakeClientFactory()
    publisher = MqttPublisher(
        config,
        bus,
        emit=emit,
        client_factory=resolved,
        reconnect_initial=0.01,
        reconnect_max=0.05,
    )
    return publisher, bus, resolved, emit


async def connected_publisher(
    config: VidetteConfig | None = None, *, factory: FakeClientFactory | None = None
) -> tuple[MqttPublisher, InProcessEventBus, FakeClientFactory, EmitRecorder]:
    publisher, bus, resolved, emit = make_publisher(config or mqtt_config(), factory=factory)
    publisher.start()
    await wait_for(lambda: publisher.status().connected)
    return publisher, bus, resolved, emit


# --- lifecycle ----------------------------------------------------------------------------------


async def test_disabled_start_spawns_nothing() -> None:
    publisher, _bus, factory, emit = make_publisher(mqtt_config(enabled=False))
    publisher.start()
    await asyncio.sleep(0)

    assert factory.clients == []  # never even built a client
    stray = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    assert stray == []
    assert publisher.status().connected is False
    assert emit.calls == []
    await publisher.stop()  # no-op, no error


async def test_connect_announces_online_and_retained_discovery_configs() -> None:
    publisher, _bus, factory, _emit = await connected_publisher()
    try:
        await wait_for(lambda: len(factory.current.publishes) >= 3)
        client = factory.current

        first = client.publishes[0]
        assert first.topic == "vidette/status"
        assert first.payload == "online"
        assert first.retain is True

        [config_call] = client.topics(
            "homeassistant/binary_sensor/vidette_front-door_person/config"
        )
        assert config_call.retain is True
        assert json.loads(as_text(config_call)) == {
            "name": "Front door person",
            "unique_id": "vidette_front-door_person",
            "state_topic": "vidette/front-door/person",
            "device_class": "occupancy",
            "payload_on": "ON",
            "payload_off": "OFF",
            "availability_topic": "vidette/status",
            "device": {
                "identifiers": ["vidette_front-door"],
                "name": "Front door",
                "manufacturer": "Vidette",
            },
        }

        # A camera without a display name falls back to its id.
        [backyard] = client.topics("homeassistant/binary_sensor/vidette_backyard_person/config")
        decoded = json.loads(as_text(backyard))
        assert decoded["name"] == "backyard person"
        assert decoded["device"]["name"] == "backyard"
    finally:
        await publisher.stop()


async def test_discovery_disabled_announces_availability_only() -> None:
    publisher, _bus, factory, _emit = await connected_publisher(mqtt_config(discovery=False))
    try:
        await wait_for(lambda: factory.current.publishes)
        topics = [call.topic for call in factory.current.publishes]
        assert topics == ["vidette/status"]
    finally:
        await publisher.stop()


# --- event → topic mapping ------------------------------------------------------------------------


async def test_confirmed_person_event_publishes_json_and_occupancy_on() -> None:
    publisher, bus, factory, _emit = await connected_publisher()
    try:
        await bus.publish("event.confirmed", dict(CONFIRMED))
        await wait_for(lambda: factory.current.topics("vidette/front-door/person"))
        client = factory.current

        [event_call] = client.topics("vidette/front-door/event")
        assert json.loads(as_text(event_call)) == CONFIRMED  # full canonical payload
        assert event_call.retain is False

        [person_call] = client.topics("vidette/front-door/person")
        assert as_text(person_call) == "ON"

        # Motion-level chatter is deliberately not bridged (events are the signal).
        assert [call for call in client.publishes if call.topic.endswith("/motion")] == []
    finally:
        await publisher.stop()


async def test_event_ended_publishes_occupancy_off() -> None:
    publisher, bus, factory, _emit = await connected_publisher()
    try:
        ended = {**CONFIRMED, "event": "event.ended", "ended_at": "2026-07-07T21:15:00.000Z"}
        await bus.publish("event.ended", ended)
        await wait_for(lambda: factory.current.topics("vidette/front-door/person"))

        [person_call] = factory.current.topics("vidette/front-door/person")
        assert as_text(person_call) == "OFF"
        assert factory.current.topics("vidette/front-door/event") == []
    finally:
        await publisher.stop()


async def test_non_person_confirmed_event_does_not_toggle_occupancy() -> None:
    publisher, bus, factory, _emit = await connected_publisher()
    try:
        vehicle = {**CONFIRMED, "kinds": ["vehicle"]}
        await bus.publish("event.confirmed", vehicle)
        await wait_for(lambda: factory.current.topics("vidette/front-door/event"))
        await asyncio.sleep(0.05)

        assert factory.current.topics("vidette/front-door/person") == []
    finally:
        await publisher.stop()


async def test_system_events_forward_to_system_topic() -> None:
    publisher, bus, factory, _emit = await connected_publisher()
    try:
        pressure = {
            "event": "system.storage.pressure",
            "free_gb": 4.2,
            "message": "retention janitor is trimming continuous footage early",
        }
        await bus.publish("system.storage.pressure", pressure)
        await wait_for(lambda: factory.current.topics("vidette/system/event"))

        [system_call] = factory.current.topics("vidette/system/event")
        assert json.loads(as_text(system_call)) == pressure
        assert system_call.retain is False
    finally:
        await publisher.stop()


# --- resilience -----------------------------------------------------------------------------------


async def test_connect_failure_reconnects_and_emits_once() -> None:
    factory = FakeClientFactory(connect_errors=[aiomqtt.MqttError("connection refused")])
    publisher, _bus, factory, emit = await connected_publisher(factory=factory)
    try:
        assert len(factory.clients) == 2  # first attempt failed, second connected
        assert factory.clients[0].entered is False
        assert factory.clients[1].entered is True
        online = [as_text(call) for call in factory.clients[1].topics("vidette/status")]
        assert online == ["online"]

        failures = emit.topics("mqtt.connection_failed")
        assert len(failures) == 1
        assert failures[0]["host"] == "broker.local"
        assert failures[0]["port"] == 1883
        assert failures[0]["attempt"] == 1
        assert "connection refused" in failures[0]["error"]
        assert "broker" in failures[0]["message"]  # actionable, names the thing to check
    finally:
        await publisher.stop()


async def test_repeated_connect_failures_emit_rate_limited() -> None:
    errors: list[Exception] = [aiomqtt.MqttError(f"refused #{n}") for n in range(7)]
    factory = FakeClientFactory(connect_errors=errors)
    publisher, _bus, factory, emit = await connected_publisher(factory=factory)
    try:
        failures = emit.topics("mqtt.connection_failed")
        assert [failure["attempt"] for failure in failures] == [1, 5]  # first + every 5th
    finally:
        await publisher.stop()


async def test_publish_error_does_not_kill_the_pump() -> None:
    publisher, bus, factory, _emit = await connected_publisher()
    try:
        factory.current.publish_errors["vidette/front-door/event"] = RuntimeError("payload bug")
        await bus.publish("event.confirmed", dict(CONFIRMED))
        await wait_for(lambda: factory.current.topics("vidette/front-door/person"))
        assert factory.current.topics("vidette/front-door/event") == []  # dropped, not fatal

        await bus.publish("event.confirmed", dict(CONFIRMED))
        await wait_for(lambda: factory.current.topics("vidette/front-door/event"))

        status = publisher.status()
        assert status.failed_total == 1
        assert status.connected is True
        assert len(factory.clients) == 1  # a per-message bug does not force a reconnect
    finally:
        await publisher.stop()


async def test_stop_publishes_offline_and_cancels_cleanly() -> None:
    publisher, bus, factory, _emit = await connected_publisher()
    client = factory.current

    await publisher.stop()
    await publisher.stop()  # idempotent

    last = client.publishes[-1]
    assert last.topic == "vidette/status"
    assert last.payload == "offline"
    assert last.retain is True
    assert client.exited is True
    assert publisher.status().connected is False

    before = len(client.publishes)
    await bus.publish("event.confirmed", dict(CONFIRMED))
    await asyncio.sleep(0.05)
    assert len(client.publishes) == before  # subscriptions closed, nothing flows

    stray = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    assert stray == []  # supervisor and consumers all cancelled
