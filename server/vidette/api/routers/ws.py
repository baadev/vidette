"""Live event stream: `WS /api/v1/ws` (docs/api.md · docs/events-and-automations.md).

Bridges the in-process event bus to WebSocket clients. Every bus message is forwarded as
one JSON text frame: `{"topic": "...", "payload": {...}}`. Clients choose what to hear
with the optional `?topics=` query — a comma-separated list of bus patterns, each of
which must start with `event.` or `system.` (`event.*`, `system.*`, `event.confirmed`,
`system.storage.*`, …). The default subscribes to everything: `event.*,system.*`.

Close codes (WebSocket application range, mirroring the HTTP status family):

- 4401: not authenticated — sent *without accepting* the handshake. Log in for a session
  cookie, or connect with an `Authorization: Bearer vd_…` header.
- 4400: invalid `topics` value — fix the pattern list and reconnect.

Authentication runs through `auth.deps.ws_principal` before `accept()` — a raising
dependency would surface as a bare handshake failure instead of the 4401 close code.

Frames received from the client are ignored (the stream is one-way); the receive loop
exists to notice disconnects. Backpressure lives in the bus (bounded queues, drop-oldest,
drops counted in `vidette_bus_dropped_total`), so a slow consumer can never stall
publishers — or the recorder.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import cast

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from vidette.auth.deps import ws_principal
from vidette.core.events import Subscription
from vidette.runtime import AppRuntime

logger = logging.getLogger(__name__)

router = APIRouter(tags=["events"])

DEFAULT_TOPICS: tuple[str, ...] = ("event.*", "system.*")
_TOPIC_PREFIXES = ("event.", "system.")

CLOSE_UNAUTHENTICATED = 4401
CLOSE_INVALID_TOPICS = 4400


def _parse_topics(raw: str | None) -> list[str] | None:
    """The pattern list to subscribe, or None when the query value is invalid.

    An absent query means the default patterns; duplicates collapse to one subscription
    per pattern.
    """
    if raw is None:
        return list(DEFAULT_TOPICS)
    patterns = list(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    if not patterns or not all(pattern.startswith(_TOPIC_PREFIXES) for pattern in patterns):
        return None
    return patterns


async def _forward(websocket: WebSocket, subscription: Subscription) -> None:
    """Pump one bus subscription into the socket as JSON text frames."""
    while True:
        topic, payload = await subscription.get()
        # default=str: bus payloads are JSON-shaped by contract (emit() persists them as
        # JSON first) — stringify any straggler rather than killing the stream over it.
        frame = json.dumps({"topic": topic, "payload": payload}, default=str)
        try:
            await websocket.send_text(frame)
        except (WebSocketDisconnect, RuntimeError):
            # The client vanished mid-send (starlette raises either, depending on how far
            # the teardown got). The receive loop owns cleanup; just stop pumping.
            logger.debug("ws forwarder for %r stopped: client disconnected", subscription.pattern)
            return


@router.websocket("/api/v1/ws")
async def live_stream(websocket: WebSocket) -> None:
    runtime = cast(AppRuntime, websocket.app.state.runtime)

    principal = await ws_principal(websocket)
    if principal is None:
        await websocket.close(
            code=CLOSE_UNAUTHENTICATED,
            reason="log in for a session cookie, or connect with Authorization: Bearer vd_…",
        )
        return

    patterns = _parse_topics(websocket.query_params.get("topics"))
    if patterns is None:
        await websocket.accept()
        await websocket.close(
            code=CLOSE_INVALID_TOPICS,
            reason="topics must be a comma-separated list of event.*/system.* patterns",
        )
        return

    # Subscribe before accepting: a message published the instant the client observes the
    # completed handshake must already have a queue to land in.
    subscriptions = [runtime.bus.subscribe(pattern) for pattern in patterns]
    forwarders: list[asyncio.Task[None]] = []
    try:
        await websocket.accept()
        forwarders = [
            asyncio.create_task(_forward(websocket, sub), name=f"vidette-ws:{sub.pattern}")
            for sub in subscriptions
        ]
        while True:
            # One-way stream: client frames carry nothing we act on. Raw receive() rather
            # than receive_text(), so a stray binary frame cannot blow up the loop.
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        pass  # client hangup surfaced as an exception — same cleanup either way
    finally:
        for subscription in subscriptions:
            subscription.close()
        for task in forwarders:
            task.cancel()
        # Collect cancellations (and any stray send error) so nothing is logged as an
        # "exception was never retrieved" after the socket is gone.
        await asyncio.gather(*forwarders, return_exceptions=True)
