"""Notification delivery: webhook contract (headers/signature/retries), Apprise rendering,
and dispatcher routing over a real in-process bus. No network, no wall-clock sleeps."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from vidette.core.config import ChannelConfig, VidetteConfig
from vidette.core.events import InProcessEventBus
from vidette.notify.apprise_channel import AppriseNotifier
from vidette.notify.dispatcher import NotificationDispatcher
from vidette.notify.signing import verify
from vidette.notify.webhook import NotifyError, WebhookNotifier

SECRET = "s3cret"
TS = 1_760_000_000
HOOK_URL = "https://automation.example.com/vidette"

PAYLOAD: dict[str, Any] = {
    "event": "event.confirmed",
    "id": "01hxv7q8e9",
    "camera": "front-door",
    "started_at": "2026-07-07T21:14:03.412Z",
    "ended_at": None,
    "kinds": ["person"],
    "zones": ["door"],
    "geometry": {"approach": 0.92, "dwell_s": 14.2, "touch": True, "repeat_pass": 0},
    "summary": "A person approached the front door and tried the handle twice.",
    "intent": {"label": "entry_attempt", "score": 0.87},
    "policy": "entry-interest",
    "media": {
        "snapshot_url": "/api/v1/events/01hxv7q8e9/snapshot.webp",
        "clip_url": "/api/v1/events/01hxv7q8e9/clip.mp4",
        "live_url": "/live/front-door",
    },
}


def webhook_channel(**overrides: Any) -> ChannelConfig:
    data: dict[str, Any] = {"kind": "webhook", "url": HOOK_URL, "secret": SECRET}
    data.update(overrides)
    return ChannelConfig.model_validate(data)


async def no_sleep(_seconds: float) -> None:
    return None


async def wait_for(condition: Callable[[], bool], timeout: float = 2.0) -> None:
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


# --- webhook ----------------------------------------------------------------------------------


async def test_webhook_headers_signature_and_include_filter() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    notifier = WebhookNotifier(transport=httpx.MockTransport(handler), clock=lambda: TS)
    channel = webhook_channel(include=["summary", "snapshot_url", "clip_url"])
    await notifier.send(channel, "event.confirmed", PAYLOAD)

    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["x-vidette-event"] == "event.confirmed"
    assert len(request.headers["x-vidette-delivery"]) == 32  # uuid4 hex
    timestamp = int(request.headers["x-vidette-timestamp"])
    assert timestamp == TS

    body = request.content
    signature = request.headers["x-vidette-signature"]
    assert signature.startswith("sha256=")
    assert verify(SECRET, timestamp, body, signature, now=TS)
    assert not verify(SECRET, timestamp, body + b" ", signature, now=TS)

    decoded = json.loads(body)
    # Identity fields survive any include list; media urls flatten to include-list names.
    assert decoded["event"] == "event.confirmed"
    assert decoded["id"] == "01hxv7q8e9"
    assert decoded["camera"] == "front-door"
    assert decoded["started_at"] == "2026-07-07T21:14:03.412Z"
    assert decoded["summary"] == PAYLOAD["summary"]
    assert decoded["snapshot_url"] == "/api/v1/events/01hxv7q8e9/snapshot.webp"
    assert decoded["clip_url"] == "/api/v1/events/01hxv7q8e9/clip.mp4"
    # Not in include → dropped.
    assert "geometry" not in decoded
    assert "zones" not in decoded
    assert "intent" not in decoded


async def test_webhook_include_governs_optional_extras_only() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(204)

    notifier = WebhookNotifier(transport=httpx.MockTransport(handler))
    await notifier.send(webhook_channel(include=["geometry"]), "event.confirmed", PAYLOAD)

    decoded = json.loads(captured[0].content)
    assert decoded["geometry"] == PAYLOAD["geometry"]
    assert "summary" not in decoded
    assert decoded["id"] == "01hxv7q8e9"  # identity fields are not filterable
    assert decoded["camera"] == "front-door"


async def test_webhook_retries_with_backoff_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200)

    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    notifier = WebhookNotifier(transport=httpx.MockTransport(handler), sleep=record_sleep)
    await notifier.send(webhook_channel(), "event.confirmed", PAYLOAD)

    assert calls == 3
    assert len(sleeps) == 2
    assert 0.5 <= sleeps[0] <= 1.0  # base 0.5 s + jitter
    assert 2.0 <= sleeps[1] <= 4.0  # base 2 s + jitter


async def test_webhook_permanent_failure_raises_actionable_error() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    notifier = WebhookNotifier(transport=httpx.MockTransport(handler), sleep=no_sleep)
    with pytest.raises(NotifyError) as excinfo:
        await notifier.send(webhook_channel(), "event.confirmed", PAYLOAD)

    assert calls == 3
    message = str(excinfo.value)
    assert "500" in message
    assert "automation.example.com" in message


# --- apprise ----------------------------------------------------------------------------------


class FakeApprise:
    def __init__(self, *, notify_result: bool = True, notify_error: Exception | None = None):
        self.added: list[str] = []
        self.notified: list[dict[str, str]] = []
        self._notify_result = notify_result
        self._notify_error = notify_error

    def add(self, url: str) -> bool:
        self.added.append(url)
        return True

    def notify(self, *, title: str, body: str) -> bool:
        self.notified.append({"title": title, "body": body})
        if self._notify_error is not None:
            raise self._notify_error
        return self._notify_result


def apprise_channel() -> ChannelConfig:
    return ChannelConfig.model_validate({"kind": "apprise", "url": "tgram://bottoken/chatid"})


async def test_apprise_sends_summary_title_and_media_link() -> None:
    fake = FakeApprise()
    notifier = AppriseNotifier(apprise_factory=lambda: fake)

    await notifier.send(apprise_channel(), "event.confirmed", PAYLOAD)

    assert fake.added == ["tgram://bottoken/chatid"]
    [message] = fake.notified
    assert message["title"] == "Vidette · front-door — event.confirmed"
    assert message["body"].startswith("A person approached the front door")
    assert "/api/v1/events/01hxv7q8e9/clip.mp4" in message["body"]


async def test_apprise_geometry_fallback_body() -> None:
    fake = FakeApprise()
    notifier = AppriseNotifier(apprise_factory=lambda: fake)
    payload = {**PAYLOAD, "summary": None, "media": {}}

    await notifier.send(apprise_channel(), "event.confirmed", payload)

    [message] = fake.notified
    assert message["body"] == "person at door — dwell 14s, touched"


async def test_apprise_failure_raises_without_leaking_url_secrets() -> None:
    fake = FakeApprise(notify_result=False)
    notifier = AppriseNotifier(apprise_factory=lambda: fake)

    with pytest.raises(NotifyError) as excinfo:
        await notifier.send(apprise_channel(), "event.confirmed", PAYLOAD)
    assert "tgram" in str(excinfo.value)
    assert "bottoken" not in str(excinfo.value)  # secrets never logged

    raising = FakeApprise(notify_error=RuntimeError("socket exploded"))
    notifier = AppriseNotifier(apprise_factory=lambda: raising)
    with pytest.raises(NotifyError):
        await notifier.send(apprise_channel(), "event.confirmed", PAYLOAD)


# --- dispatcher -------------------------------------------------------------------------------


def dispatcher_config(
    *,
    base_url: str | None = None,
    channels: dict[str, Any] | None = None,
    rules: list[dict[str, Any]] | None = None,
) -> VidetteConfig:
    if channels is None:
        channels = {
            "hooks": {
                "kind": "webhook",
                "url": HOOK_URL,
                "secret": SECRET,
                "include": ["summary", "clip_url"],
            }
        }
    if rules is None:
        rules = [
            {"when": "event.confirmed", "channels": ["hooks"]},
            {"when": "system.*", "channels": ["hooks"]},
        ]
    data: dict[str, Any] = {"notifications": {"channels": channels, "rules": rules}}
    if base_url is not None:
        data["server"] = {"base_url": base_url}
    return VidetteConfig.model_validate(data)


def recording_transport(received: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200)

    return httpx.MockTransport(handler)


async def test_dispatcher_routes_matching_topics_only() -> None:
    received: list[httpx.Request] = []
    bus = InProcessEventBus()
    emit = EmitRecorder()
    dispatcher = NotificationDispatcher(
        dispatcher_config(),
        bus,
        emit=emit,
        notifiers={"webhook": WebhookNotifier(transport=recording_transport(received))},
    )
    dispatcher.start()
    try:
        await bus.publish("event.confirmed", dict(PAYLOAD))
        await wait_for(lambda: len(received) == 1)

        await bus.publish(
            "system.storage.pressure",
            {"event": "system.storage.pressure", "id": "sys-1", "camera": "-"},
        )
        await wait_for(lambda: len(received) == 2)

        await bus.publish("event.dismissed", {**PAYLOAD, "event": "event.dismissed"})
        await asyncio.sleep(0.05)
        assert len(received) == 2  # dismissed events stay silent

        status = dispatcher.status()
        assert status.delivered_total == 2
        assert status.failed_total == 0
        assert status.per_channel["hooks"].delivered == 2
        assert emit.calls == []
    finally:
        await dispatcher.stop()


async def test_dispatcher_rewrites_relative_media_to_base_url() -> None:
    received: list[httpx.Request] = []
    bus = InProcessEventBus()
    dispatcher = NotificationDispatcher(
        dispatcher_config(base_url="https://vidette.local/"),
        bus,
        emit=EmitRecorder(),
        notifiers={"webhook": WebhookNotifier(transport=recording_transport(received))},
    )
    dispatcher.start()
    try:
        await bus.publish("event.confirmed", dict(PAYLOAD))
        await wait_for(lambda: len(received) == 1)
        decoded = json.loads(received[0].content)
        assert decoded["clip_url"] == "https://vidette.local/api/v1/events/01hxv7q8e9/clip.mp4"
    finally:
        await dispatcher.stop()


async def test_dispatcher_skips_webpush_with_one_notice_per_boot() -> None:
    received: list[httpx.Request] = []
    bus = InProcessEventBus()
    emit = EmitRecorder()
    config = dispatcher_config(
        channels={
            "push": {"kind": "webpush"},
            "hooks": {"kind": "webhook", "url": HOOK_URL, "secret": SECRET},
        },
        rules=[{"when": "event.*", "channels": ["push", "hooks"]}],
    )
    dispatcher = NotificationDispatcher(
        config,
        bus,
        emit=emit,
        notifiers={"webhook": WebhookNotifier(transport=recording_transport(received))},
    )
    dispatcher.start()
    try:
        await bus.publish("event.confirmed", dict(PAYLOAD))
        await bus.publish("event.enriched", {**PAYLOAD, "event": "event.enriched"})
        await wait_for(lambda: len(received) == 2)

        notices = emit.topics("notify.webpush_unavailable")
        assert len(notices) == 1  # once per boot, not per message
        assert notices[0]["channel"] == "push"
        assert "M2" in notices[0]["message"]
        assert "push" not in dispatcher.status().per_channel  # skipped, not failed
    finally:
        await dispatcher.stop()


class FailingNotifier:
    kind = "apprise"

    def __init__(self) -> None:
        self.attempts = 0

    async def send(self, channel: ChannelConfig, topic: str, payload: dict[str, Any]) -> None:
        self.attempts += 1
        raise NotifyError("simulated outage")


async def test_dispatcher_survives_failing_channel_and_emits_rate_limited() -> None:
    received: list[httpx.Request] = []
    bus = InProcessEventBus()
    emit = EmitRecorder()
    failing = FailingNotifier()
    config = dispatcher_config(
        channels={
            "bad": {"kind": "apprise", "url": "ntfy://demo"},
            "hooks": {"kind": "webhook", "url": HOOK_URL, "secret": SECRET},
        },
        rules=[{"when": "event.*", "channels": ["bad", "hooks"]}],
    )
    dispatcher = NotificationDispatcher(
        config,
        bus,
        emit=emit,
        notifiers={
            "webhook": WebhookNotifier(transport=recording_transport(received)),
            "apprise": failing,
        },
    )
    dispatcher.start()
    try:
        await bus.publish("event.confirmed", dict(PAYLOAD))
        await bus.publish("event.enriched", {**PAYLOAD, "event": "event.enriched"})
        await wait_for(lambda: len(received) == 2 and failing.attempts == 2)

        failures = emit.topics("notify.delivery_failed")
        assert len(failures) == 1  # first failure emitted, second rate-limited
        assert failures[0]["channel"] == "bad"
        assert failures[0]["topic"] == "event.confirmed"
        assert "simulated outage" in failures[0]["error"]

        status = dispatcher.status()
        assert status.per_channel["bad"].failed == 2
        assert status.per_channel["hooks"].delivered == 2
        assert status.failed_total == 2
        assert status.delivered_total == 2
    finally:
        await dispatcher.stop()


async def test_dispatcher_stop_terminates_cleanly() -> None:
    received: list[httpx.Request] = []
    bus = InProcessEventBus()
    dispatcher = NotificationDispatcher(
        dispatcher_config(),
        bus,
        emit=EmitRecorder(),
        notifiers={"webhook": WebhookNotifier(transport=recording_transport(received))},
    )
    dispatcher.start()
    await bus.publish("event.confirmed", dict(PAYLOAD))
    await wait_for(lambda: len(received) == 1)

    await dispatcher.stop()
    await dispatcher.stop()  # idempotent

    await bus.publish("event.confirmed", dict(PAYLOAD))
    await asyncio.sleep(0.05)
    assert len(received) == 1  # subscriptions are closed, nothing delivered

    stray = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    assert stray == []  # no pending dispatcher tasks after stop
