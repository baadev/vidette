"""Signed webhook delivery — implements the binding contract in
docs/events-and-automations.md#webhooks.

POST, JSON body, 10 s timeout, 3 attempts with exponential backoff + jitter. Headers:
`X-Vidette-Event`, `X-Vidette-Delivery`, `X-Vidette-Timestamp` (unix seconds) and
`X-Vidette-Signature` (see vidette.notify.signing — the tested reference implementation).
The delivery id is stable across retries so receivers can deduplicate.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from vidette.core.config import ChannelConfig
from vidette.notify.signing import sign

# Identity fields every receiver needs for idempotency — always present, not filterable.
ALWAYS_INCLUDED = ("event", "id", "camera", "started_at")
# Optional extras the channel's `include` list opts into.
INCLUDABLE = (
    "summary",
    "snapshot_url",
    "clip_url",
    "geometry",
    "zones",
    "kinds",
    "policy",
    "intent",
)

_ATTEMPTS = 3
_BACKOFF_BASE_S = (0.5, 2.0)  # wait after attempt 1 / attempt 2; jitter doubles at most
_TIMEOUT_S = 10.0


class NotifyError(Exception):
    """Delivery failed after retries; the message says which receiver/config to check."""


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def filter_payload(payload: dict[str, Any], include: list[str]) -> dict[str, Any]:
    """Reduce the canonical event payload to identity fields + the channel's include list.

    `snapshot_url` / `clip_url` live under the nested `media` object of the canonical shape;
    they are flattened to top-level keys so the body mirrors the include-list names.
    """
    media = payload.get("media")
    media = media if isinstance(media, dict) else {}
    body: dict[str, Any] = {}
    for name in ALWAYS_INCLUDED:
        if name in payload:
            body[name] = payload[name]
    for name in INCLUDABLE:
        if name not in include:
            continue
        if name in payload:
            body[name] = payload[name]
        elif name in media:
            body[name] = media[name]
    return body


class WebhookNotifier:
    """Notifier for `kind: webhook` channels. `transport`, `clock` and `sleep` are injectable
    so tests never touch the network or the wall clock."""

    kind = "webhook"

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._sleep = sleep if sleep is not None else _default_sleep

    async def send(self, channel: ChannelConfig, topic: str, payload: dict[str, Any]) -> None:
        if not channel.url or not channel.secret:
            raise NotifyError(
                "webhook channel is missing 'url' or 'secret' — check notifications.channels"
            )
        body = json.dumps(
            filter_payload(payload, channel.include), separators=(",", ":"), default=str
        ).encode()
        delivery_id = uuid4().hex
        host = urlsplit(channel.url).netloc or channel.url
        last_error = "no attempt made"

        async with httpx.AsyncClient(transport=self._transport, timeout=_TIMEOUT_S) as client:
            for attempt in range(_ATTEMPTS):
                # Timestamp + signature are refreshed per attempt: backoff must not push the
                # delivery outside the receiver's replay window.
                timestamp = int(self._clock())
                headers = {
                    "Content-Type": "application/json",
                    "X-Vidette-Event": topic,
                    "X-Vidette-Delivery": delivery_id,
                    "X-Vidette-Timestamp": str(timestamp),
                    "X-Vidette-Signature": sign(channel.secret, timestamp, body),
                }
                try:
                    response = await client.post(channel.url, content=body, headers=headers)
                except httpx.HTTPError as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                else:
                    if response.is_success:
                        return
                    last_error = f"HTTP {response.status_code}"
                if attempt < _ATTEMPTS - 1:
                    base = _BACKOFF_BASE_S[attempt]
                    await self._sleep(base * (1.0 + random.random()))

        raise NotifyError(
            f"webhook delivery to {host} failed after {_ATTEMPTS} attempts ({last_error}) — "
            "check that the receiver is reachable and the channel url/secret are correct"
        )
