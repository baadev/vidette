"""Apprise fan-out — one URL scheme per destination (Telegram, Discord, ntfy, email, …).

Apprise's `notify()` is synchronous, so it runs in a worker thread: delivery must never
block the event loop (recording is sacred). Apprise URLs embed credentials, so error
messages only ever mention the scheme — secrets never reach logs or system events.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import apprise

from vidette.core.config import ChannelConfig
from vidette.notify.webhook import NotifyError


def _scheme(url: str) -> str:
    return url.split("://", 1)[0] if "://" in url else "unknown"


def _humane_line(payload: dict[str, Any]) -> str:
    """Geometry alerts stand on their own: 'person at door — dwell 14s, touched'."""
    kinds = payload.get("kinds") or []
    zones = payload.get("zones") or []
    geometry = payload.get("geometry")
    geometry = geometry if isinstance(geometry, dict) else {}

    subject = ", ".join(str(kind) for kind in kinds) or "activity"
    where = f" at {', '.join(str(zone) for zone in zones)}" if zones else ""

    facts: list[str] = []
    dwell = geometry.get("dwell_s")
    if dwell:
        facts.append(f"dwell {round(float(dwell))}s")
    if geometry.get("touch"):
        facts.append("touched")
    if geometry.get("loiter"):
        facts.append("loitering")
    repeat = geometry.get("repeat_pass") or 0
    if repeat:
        facts.append(f"passed {repeat}×")

    line = f"{subject}{where}"
    if facts:
        line += " — " + ", ".join(facts)
    return line


class AppriseNotifier:
    """Notifier for `kind: apprise` channels. `apprise_factory` is injectable for tests."""

    kind = "apprise"

    def __init__(self, *, apprise_factory: Callable[[], Any] | None = None) -> None:
        self._factory: Callable[[], Any] = (
            apprise_factory if apprise_factory is not None else apprise.Apprise
        )

    async def send(self, channel: ChannelConfig, topic: str, payload: dict[str, Any]) -> None:
        if not channel.url:
            raise NotifyError("apprise channel is missing 'url' — check notifications.channels")
        scheme = _scheme(channel.url)

        camera = payload.get("camera") or "unknown"
        title = f"Vidette · {camera} — {topic}"
        summary = payload.get("summary")
        body = str(summary) if summary else _humane_line(payload)
        media = payload.get("media")
        media = media if isinstance(media, dict) else {}
        link = media.get("clip_url") or media.get("live_url") or payload.get("clip_url")
        if link:
            body = f"{body}\n{link}"

        client = self._factory()
        if client.add(channel.url) is False:
            raise NotifyError(
                f"apprise rejected the '{scheme}://' url — check the channel url against "
                "the Apprise scheme documentation"
            )
        try:
            delivered = await asyncio.to_thread(client.notify, title=title, body=body)
        except Exception as exc:  # apprise plugins raise arbitrary errors
            raise NotifyError(
                f"apprise delivery via '{scheme}://' raised {type(exc).__name__} — "
                "check the destination service and the channel url"
            ) from exc
        if not delivered:
            raise NotifyError(
                f"apprise delivery via '{scheme}://' failed — check the destination "
                "service credentials in the channel url"
            )
