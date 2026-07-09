"""Web push (VAPID): key minting, notifier fan-out + pruning, the push API, dispatcher wiring.

Everything runs against a real tmp-file Database (the conftest `db` fixture); deliveries go
through a recording fake in place of `pywebpush.webpush`, so no push service is touched.
Router tests drive the app through httpx's ASGI transport (not TestClient) so the app and
the fixtures share one event loop — the Database write lock binds to whichever loop uses it
first.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pywebpush import WebPushException  # type: ignore[import-untyped]

import vidette.auth.deps as auth_deps
from vidette.auth.service import Principal
from vidette.core.config import ChannelConfig, VidetteConfig
from vidette.core.events import InProcessEventBus
from vidette.db import Database
from vidette.notify.dispatcher import NotificationDispatcher
from vidette.notify.webhook import NotifyError
from vidette.notify.webpush import WebPushNotifier, ensure_vapid_keys

TOPIC = "event.confirmed"

PAYLOAD: dict[str, Any] = {
    "event": "event.confirmed",
    "id": "01hxv7q8e9",
    "camera": "front-door",
    "kinds": ["person"],
    "zones": ["door"],
    "geometry": {"approach": 0.92, "dwell_s": 14.2, "touch": True, "repeat_pass": 0},
    "summary": "A person approached the front door and tried the handle twice.",
    "media": {
        "snapshot_url": "/api/v1/events/01hxv7q8e9/snapshot.webp",
        "live_url": "https://vidette.local/live/front-door",
    },
}


def subscription(name: str) -> dict[str, Any]:
    """What `subscription.toJSON()` yields in the browser."""
    return {
        "endpoint": f"https://push.example.com/send/{name}",
        "expirationTime": None,
        "keys": {"p256dh": f"p256dh-{name}", "auth": f"auth-{name}"},
    }


SUB_A = subscription("aaa")
SUB_B = subscription("bbb")


def push_channel() -> ChannelConfig:
    return ChannelConfig.model_validate({"kind": "webpush"})


async def seed_user(db: Database) -> int:
    return await db.create_user("alex", "scrypt$not-a-real-hash", role="admin")


async def seed_subscriptions(db: Database, *subs: dict[str, Any]) -> int:
    user_id = await seed_user(db)
    for sub in subs:
        await db.upsert_push_subscription(sub["endpoint"], sub, user_id)
    return user_id


class FakeWebPush:
    """Records `pywebpush.webpush` keyword calls; raises per-endpoint on demand."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.gone: set[str] = set()  # endpoints answering 410 Gone
        self.failing: set[str] = set()  # endpoints failing without a response

    def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        endpoint = kwargs["subscription_info"]["endpoint"]
        if endpoint in self.gone:
            raise WebPushException("gone", response=SimpleNamespace(status_code=410))
        if endpoint in self.failing:
            raise WebPushException("push service unreachable")


# --- VAPID keys ---------------------------------------------------------------------------------


async def test_ensure_vapid_keys_mints_once_in_browser_format(db: Database) -> None:
    private_pem, public_key = await ensure_vapid_keys(db)

    assert "BEGIN PRIVATE KEY" in private_pem
    assert "=" not in public_key  # base64url without padding
    point = base64.urlsafe_b64decode(public_key + "=" * (-len(public_key) % 4))
    assert len(point) == 65
    assert point[0] == 0x04  # uncompressed EC point marker

    assert await db.get_meta("vapid_private_pem") == private_pem
    assert await db.get_meta("vapid_public_key") == public_key
    # Later calls return the stored pair unchanged — subscriptions depend on a stable key.
    assert await ensure_vapid_keys(db) == (private_pem, public_key)


# --- notifier -----------------------------------------------------------------------------------


async def test_notifier_fans_out_to_every_subscription(db: Database) -> None:
    await seed_subscriptions(db, SUB_A, SUB_B)
    fake = FakeWebPush()
    notifier = WebPushNotifier(db, webpush_fn=fake)

    await notifier.send(push_channel(), TOPIC, PAYLOAD)

    endpoints = {call["subscription_info"]["endpoint"] for call in fake.calls}
    assert endpoints == {SUB_A["endpoint"], SUB_B["endpoint"]}
    private_pem = await db.get_meta("vapid_private_pem")
    for call in fake.calls:
        assert call["vapid_private_key"] == private_pem
        assert call["vapid_claims"] == {"sub": "mailto:alex@baadev.com"}
        assert call["ttl"] == 60
        assert json.loads(call["data"]) == {
            "title": "Vidette · front-door",
            "body": PAYLOAD["summary"],
            "url": "https://vidette.local/live/front-door",
            "topic": "event.confirmed",
        }
    # pywebpush mutates the claims dict (audience cache) — every delivery gets a fresh one.
    assert fake.calls[0]["vapid_claims"] is not fake.calls[1]["vapid_claims"]


async def test_notifier_geometry_fallback_body_and_default_link(db: Database) -> None:
    await seed_subscriptions(db, SUB_A)
    fake = FakeWebPush()
    payload = {**PAYLOAD, "summary": None, "media": {}}

    await WebPushNotifier(db, webpush_fn=fake).send(push_channel(), TOPIC, payload)

    [call] = fake.calls
    body = json.loads(call["data"])
    assert body["body"] == "person at door — dwell 14s, touched"
    assert body["url"] == "/#/events"


async def test_notifier_zero_subscriptions_is_a_silent_noop(db: Database) -> None:
    fake = FakeWebPush()

    await WebPushNotifier(db, webpush_fn=fake).send(push_channel(), TOPIC, PAYLOAD)

    assert fake.calls == []
    assert await db.get_meta("vapid_private_pem") is None  # keys stay unminted until needed


async def test_gone_subscription_is_pruned_while_the_rest_deliver(db: Database) -> None:
    await seed_subscriptions(db, SUB_A, SUB_B)
    fake = FakeWebPush()
    fake.gone.add(SUB_A["endpoint"])

    # One delivered, one pruned: not a failure, so nothing raises.
    await WebPushNotifier(db, webpush_fn=fake).send(push_channel(), TOPIC, PAYLOAD)

    remaining = [row.endpoint for row in await db.list_push_subscriptions()]
    assert remaining == [SUB_B["endpoint"]]


async def test_partial_failure_is_swallowed_but_total_failure_raises(db: Database) -> None:
    await seed_subscriptions(db, SUB_A, SUB_B)
    fake = FakeWebPush()
    notifier = WebPushNotifier(db, webpush_fn=fake)

    fake.failing.add(SUB_A["endpoint"])
    await notifier.send(push_channel(), TOPIC, PAYLOAD)  # one still delivered → no raise

    fake.failing.add(SUB_B["endpoint"])
    with pytest.raises(NotifyError) as excinfo:
        await notifier.send(push_channel(), TOPIC, PAYLOAD)

    message = str(excinfo.value)
    assert "all 2 web-push deliveries failed" in message
    assert "VAPID" in message
    assert len(await db.list_push_subscriptions()) == 2  # failures are never pruned


# --- the push API -------------------------------------------------------------------------------


def push_app(db: Database, user_id: int) -> FastAPI:
    from vidette.api.routers.push import router

    app = FastAPI()
    app.include_router(router)
    app.state.runtime = SimpleNamespace(db=db)
    principal = Principal(
        user_id=user_id, username="alex", role="admin", scopes=frozenset({"admin"}), via="session"
    )
    app.dependency_overrides[auth_deps.current_principal] = lambda: principal
    return app


def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test")


async def test_vapid_key_endpoint_mints_and_returns_the_stored_key(db: Database) -> None:
    app = push_app(db, await seed_user(db))
    async with client_for(app) as client:
        first = await client.get("/api/v1/push/vapid-key")
        again = await client.get("/api/v1/push/vapid-key")

    assert first.status_code == 200
    key = first.json()["key"]
    assert key == await db.get_meta("vapid_public_key")
    assert again.json() == {"key": key}


async def test_subscription_roundtrip_upserts_for_the_caller(db: Database) -> None:
    user_id = await seed_user(db)
    app = push_app(db, user_id)
    async with client_for(app) as client:
        created = await client.post("/api/v1/push/subscriptions", json=SUB_A)
        assert created.status_code == 204

        [row] = await db.list_push_subscriptions()
        assert row.endpoint == SUB_A["endpoint"]
        assert row.subscription == SUB_A  # stored verbatim — pywebpush needs it as-is
        assert row.user_id == user_id

        deleted = await client.request(
            "DELETE", "/api/v1/push/subscriptions", json={"endpoint": SUB_A["endpoint"]}
        )
        assert deleted.status_code == 204
        assert await db.list_push_subscriptions() == []

        missing = await client.request(
            "DELETE", "/api/v1/push/subscriptions", json={"endpoint": SUB_A["endpoint"]}
        )
        assert missing.status_code == 404
        detail = missing.json()["detail"]
        assert detail["type"] == "about:blank"
        assert detail["title"] == "Subscription not found"


@pytest.mark.parametrize(
    "body",
    [
        {"keys": {"p256dh": "pk", "auth": "a"}},  # endpoint missing
        {"endpoint": 42, "keys": {"p256dh": "pk", "auth": "a"}},  # endpoint not a string
        {"endpoint": "https://push.example.com/x", "keys": "nope"},  # keys not an object
        {"endpoint": "https://push.example.com/x", "keys": {"p256dh": "pk"}},  # auth missing
    ],
)
async def test_malformed_subscription_is_a_422_problem(db: Database, body: dict[str, Any]) -> None:
    app = push_app(db, await seed_user(db))
    async with client_for(app) as client:
        response = await client.post("/api/v1/push/subscriptions", json=body)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["type"] == "about:blank"
    assert "PushSubscription" in detail["detail"]
    assert await db.list_push_subscriptions() == []


# --- dispatcher wiring --------------------------------------------------------------------------


class RecordingWebPushNotifier:
    kind = "webpush"

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def send(self, channel: ChannelConfig, topic: str, payload: dict[str, Any]) -> None:
        self.sent.append((topic, payload))


async def wait_for(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(0.01)


async def test_dispatcher_delivers_to_registered_webpush_notifier() -> None:
    config = VidetteConfig.model_validate(
        {
            "notifications": {
                "channels": {"push": {"kind": "webpush"}},
                "rules": [{"when": "event.confirmed", "channels": ["push"]}],
            }
        }
    )
    bus = InProcessEventBus()
    emits: list[tuple[str, dict[str, Any]]] = []

    async def emit(topic: str, payload: dict[str, Any]) -> None:
        emits.append((topic, payload))

    notifier = RecordingWebPushNotifier()
    dispatcher = NotificationDispatcher(config, bus, emit=emit, notifiers={"webpush": notifier})
    dispatcher.start()
    try:
        await bus.publish("event.confirmed", dict(PAYLOAD))
        await wait_for(lambda: len(notifier.sent) == 1)

        topic, delivered = notifier.sent[0]
        assert topic == "event.confirmed"
        assert delivered["id"] == PAYLOAD["id"]
        # Registered notifier → delivered, not skipped: no unavailability notice.
        assert [name for name, _ in emits if name == "notify.webpush_unavailable"] == []
        assert dispatcher.status().per_channel["push"].delivered == 1
    finally:
        await dispatcher.stop()
