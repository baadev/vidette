"""FrigateSource: recorded MQTT sessions in, typed incidents on the bus out.

Sessions are replayed through `handle()` (no broker); the connection loop is exercised
with a fake client that implements the `MqttSubscriber` seam.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import TracebackType
from typing import Any

from conftest import FIXTURES

from vidette.core.config import FrigateConfig
from vidette.core.events import InProcessEventBus, Message
from vidette.core.incidents import Incident, TrackedObject
from vidette.frigate.source import (
    FrigateSource,
    iter_recording,
    record_line,
    replay,
    topic_text,
)

SESSIONS = FIXTURES / "frigate"
FRONT_DOOR = SESSIONS / "session-front-door.jsonl"
STREET = SESSIONS / "session-street.jsonl"


def frigate_config(**overrides: Any) -> FrigateConfig:
    return FrigateConfig.model_validate(
        {"url": "http://frigate:5000", "mqtt": {"host": "broker"}, **overrides}
    )


class EmitRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, kind: str, payload: dict[str, Any]) -> None:
        self.calls.append((kind, payload))


def drain(bus: InProcessEventBus, pattern: str) -> Callable[[], list[Message]]:
    subscription = bus.subscribe(pattern)

    def take() -> list[Message]:
        out: list[Message] = []
        while not subscription._queue.empty():
            out.append(subscription._queue.get_nowait())
        return out

    return take


async def run_session(
    path: Path, config: FrigateConfig | None = None
) -> tuple[FrigateSource, list[Message], list[Message], EmitRecorder]:
    bus = InProcessEventBus()
    incidents = drain(bus, "incident.*")
    objects = drain(bus, "object.*")
    emit = EmitRecorder()
    source = FrigateSource(config or frigate_config(), bus, emit=emit)
    await replay(source, path)
    return source, incidents(), objects(), emit


async def test_front_door_session_becomes_one_incident_lifecycle() -> None:
    source, incidents, objects, emit = await run_session(FRONT_DOOR)

    assert [topic for topic, _ in incidents] == [
        "incident.opened",
        "incident.updated",
        "incident.closed",
        "incident.summarized",
    ]
    opened = Incident.model_validate(incidents[0][1])
    updated = Incident.model_validate(incidents[1][1])
    closed = Incident.model_validate(incidents[2][1])
    summarized = Incident.model_validate(incidents[3][1])
    assert opened.key == updated.key == closed.key == summarized.key
    assert opened.severity == "detection" and updated.severity == "alert"
    assert updated.zones == ["path", "porch"]
    assert closed.ended_at is not None
    assert summarized.upstream_summary is not None
    assert summarized.upstream_summary.threat_level == 1

    assert [topic for topic, _ in objects] == [
        "object.opened",
        "object.updated",
        "object.updated",
        "object.closed",
    ]
    assert TrackedObject.model_validate(objects[2][1]).stationary is True

    status = source.status()
    assert status.messages_total == 9
    assert status.incidents_total == 1
    assert status.invalid_total == 0
    assert status.frigate_available is True
    assert emit.calls == []  # Frigate online at startup is not news


async def test_severity_filter_opens_on_the_upgrade_not_before() -> None:
    """With severities [alert], the review is opened when Frigate upgrades it."""
    _source, incidents, _objects, _emit = await run_session(
        FRONT_DOOR, frigate_config(severities=["alert"])
    )
    topics = [topic for topic, _ in incidents]
    assert topics == ["incident.opened", "incident.closed", "incident.summarized"]
    assert Incident.model_validate(incidents[0][1]).severity == "alert"


async def test_passer_by_detection_is_dropped_by_an_alert_only_filter() -> None:
    _source, incidents, _objects, _emit = await run_session(
        STREET, frigate_config(severities=["alert"])
    )
    assert incidents == []


async def test_false_positive_objects_are_skipped() -> None:
    _source, _incidents, objects, _emit = await run_session(STREET)
    labels = [TrackedObject.model_validate(payload).label for _, payload in objects]
    assert "dog" not in labels
    assert labels == ["person", "person"]


async def test_camera_filter() -> None:
    _source, incidents, objects, _emit = await run_session(
        FRONT_DOOR, frigate_config(cameras=["street"])
    )
    assert incidents == [] and objects == []


async def test_tracked_objects_can_be_turned_off() -> None:
    source, incidents, objects, _emit = await run_session(
        FRONT_DOOR, frigate_config(tracked_objects=False)
    )
    assert objects == []  # not subscribed live, and ignored on replay too
    assert "frigate/events" not in source.topics
    assert incidents  # reviews still flow


async def test_frigate_going_offline_and_back_is_reported_once_each() -> None:
    _source, _incidents, _objects, emit = await run_session(STREET)
    assert [kind for kind, _ in emit.calls] == ["frigate.unavailable", "frigate.available"]
    assert "offline" in emit.calls[0][1]["message"]


async def test_malformed_payloads_are_counted_reported_and_survived() -> None:
    source, incidents, _objects, emit = await run_session(SESSIONS / "malformed.jsonl")
    status = source.status()
    assert status.invalid_total == 2
    assert [topic for topic, _ in incidents] == ["incident.opened"]  # the valid one after
    [(kind, payload)] = emit.calls  # rate-limited: the 1st invalid payload is reported
    assert kind == "frigate.payload_invalid"
    assert "camera" in payload["error"]
    assert "0.14" in payload["message"]


async def test_end_for_an_unseen_review_still_closes() -> None:
    """Vidette restarted mid-review: the end message alone must still be usable."""
    bus = InProcessEventBus()
    incidents = drain(bus, "incident.*")
    source = FrigateSource(frigate_config(), bus, emit=EmitRecorder())
    end_lines = [
        (topic, raw)
        for topic, raw in iter_recording(FRONT_DOOR)
        if topic.endswith("/reviews") and b'"type": "end"' in raw
    ]
    for topic, raw in end_lines:
        await source.handle(topic, raw)
    assert [topic for topic, _ in incidents()] == ["incident.closed"]


async def test_custom_topic_prefix() -> None:
    bus = InProcessEventBus()
    incidents = drain(bus, "incident.*")
    source = FrigateSource(
        frigate_config(mqtt={"host": "broker", "topic_prefix": "nvr"}), bus, emit=EmitRecorder()
    )
    assert source.topics == ["nvr/available", "nvr/reviews", "nvr/events"]
    for topic, raw in iter_recording(FRONT_DOOR):
        await source.handle(topic.replace("frigate/", "nvr/"), raw)
    assert len(incidents()) == 4
    await source.handle("frigate/reviews", b"{}")  # another prefix: not ours, ignored
    assert source.status().invalid_total == 0


def test_record_line_round_trips_through_iter_recording(tmp_path: Path) -> None:
    path = tmp_path / "rec.jsonl"
    path.write_text(
        record_line("frigate/available", b"online", at=1.0)
        + "\n"
        + record_line("frigate/reviews", b'{"type": "new"}', at=2.0)
        + "\n",
        encoding="utf-8",
    )
    assert list(iter_recording(path)) == [
        ("frigate/available", b"online"),
        ("frigate/reviews", b'{"type": "new"}'),
    ]


def test_topic_text_accepts_aiomqtt_topics_and_strings() -> None:
    class FakeTopic:
        value = "frigate/reviews"

    assert topic_text(FakeTopic()) == "frigate/reviews"
    assert topic_text("frigate/events") == "frigate/events"


# --- connection loop ------------------------------------------------------------------------------


class FakeMessage:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


class FakeSubscriber:
    """Delivers queued messages, then idles like a quiet broker connection."""

    def __init__(self, messages: list[tuple[str, bytes]], connect_error: Exception | None) -> None:
        self._messages = messages
        self._connect_error = connect_error
        self.subscribed: list[str] = []

    async def __aenter__(self) -> FakeSubscriber:
        if self._connect_error is not None:
            raise self._connect_error
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        return None

    async def subscribe(self, topic: str, qos: int = 0) -> object:
        self.subscribed.append(topic)
        return None

    @property
    def messages(self) -> AsyncIterator[FakeMessage]:
        async def gen() -> AsyncIterator[FakeMessage]:
            for topic, raw in self._messages:
                yield FakeMessage(topic, raw)
            await asyncio.Event().wait()

        return gen()


async def wait_for(condition: Callable[[], object], timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(0.01)


async def test_supervisor_retries_after_connection_failure_then_streams() -> None:
    attempts: list[FakeSubscriber] = []
    recorded = list(iter_recording(FRONT_DOOR))

    def factory() -> FakeSubscriber:
        error = ConnectionRefusedError("broker down") if not attempts else None
        client = FakeSubscriber(recorded, error)
        attempts.append(client)
        return client

    bus = InProcessEventBus()
    incidents = drain(bus, "incident.*")
    emit = EmitRecorder()
    source = FrigateSource(
        frigate_config(),
        bus,
        emit=emit,
        client_factory=factory,
        reconnect_initial=0.01,
        reconnect_max=0.02,
    )
    source.start()
    try:
        await wait_for(lambda: source.status().messages_total == len(recorded))
        assert source.status().connected is True
        assert attempts[1].subscribed == [
            "frigate/available",
            "frigate/reviews",
            "frigate/events",
        ]
        assert [kind for kind, _ in emit.calls] == ["frigate.mqtt_connection_failed"]
        assert "broker down" in emit.calls[0][1]["error"]
        assert len(incidents()) == 4
    finally:
        await source.stop()
    assert source.status().connected is False


def test_cli_replay_prints_one_line_per_phase(capsys: Any) -> None:
    from vidette.frigate import cli

    assert cli.cmd_replay(FRONT_DOOR, None, "frigate") == 0
    out = capsys.readouterr().out
    assert out.count("incident.") == 4
    assert "threat=1 Person tries the front door" in out
    assert "9 messages, 1 incidents opened, 0 invalid" in out
    assert cli.cmd_replay(SESSIONS / "malformed.jsonl", None, "frigate") == 1
