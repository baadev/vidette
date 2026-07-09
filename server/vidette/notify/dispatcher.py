"""Notification dispatcher — subscribes configured rules to the event bus and fans out.

Design constraints (CLAUDE.md prime directives):
- delivery can never crash or stall anything upstream: every failure is caught and becomes
  a rate-limited `notify.delivery_failed` event — the system snitches on itself;
- honesty: configured `webpush` channels are skipped with a one-time
  `notify.webpush_unavailable` event instead of silently pretending (web push lands in M2);
- ordering is per receiver: one asyncio.Lock per channel, while channels of one message are
  delivered concurrently.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from vidette.core.config import ChannelKind, NotifyRule, VidetteConfig
from vidette.core.events import InProcessEventBus, Subscription
from vidette.notify.apprise_channel import AppriseNotifier
from vidette.notify.base import Notifier
from vidette.notify.webhook import NotifyError, WebhookNotifier

_FAILURE_EMIT_EVERY = 5  # emit on the 1st consecutive failure, then every 5th


@dataclass
class ChannelCounters:
    delivered: int = 0
    failed: int = 0


@dataclass
class DispatcherStatus:
    delivered_total: int
    failed_total: int
    per_channel: dict[str, ChannelCounters]


class NotificationDispatcher:
    """Routes bus messages matching `notifications.rules` to their channels' notifiers."""

    def __init__(
        self,
        config: VidetteConfig,
        bus: InProcessEventBus,
        *,
        emit: Callable[[str, dict[str, Any]], Awaitable[None]],
        notifiers: dict[str, Notifier] | None = None,
        base_url: str | None = None,
    ) -> None:
        self._config = config
        self._bus = bus
        self._emit = emit
        self._notifiers: dict[str, Notifier] = (
            notifiers
            if notifiers is not None
            else {
                WebhookNotifier.kind: WebhookNotifier(),
                AppriseNotifier.kind: AppriseNotifier(),
            }
        )
        resolved = base_url if base_url is not None else config.server.base_url
        self._base_url = resolved.rstrip("/") if resolved else None
        self._tasks: list[asyncio.Task[None]] = []
        self._subscriptions: list[Subscription] = []
        self._locks: dict[str, asyncio.Lock] = {}
        self._counters: dict[str, ChannelCounters] = {}
        self._failure_streaks: dict[str, int] = {}
        self._webpush_warned = False
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        for rule in self._config.notifications.rules:
            subscription = self._bus.subscribe(rule.when)
            self._subscriptions.append(subscription)
            self._tasks.append(
                asyncio.create_task(self._pump(rule, subscription), name=f"notify:{rule.when}")
            )

    async def stop(self) -> None:
        for subscription in self._subscriptions:
            subscription.close()
        self._subscriptions.clear()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        self._started = False

    def status(self) -> DispatcherStatus:
        per_channel = {
            name: ChannelCounters(counters.delivered, counters.failed)
            for name, counters in self._counters.items()
        }
        return DispatcherStatus(
            delivered_total=sum(item.delivered for item in per_channel.values()),
            failed_total=sum(item.failed for item in per_channel.values()),
            per_channel=per_channel,
        )

    async def _pump(self, rule: NotifyRule, subscription: Subscription) -> None:
        while True:
            topic, payload = await subscription.get()
            # Concurrent across a message's channels; per-channel locks keep receiver order.
            await asyncio.gather(*(self._deliver(name, topic, payload) for name in rule.channels))

    async def _deliver(self, channel_name: str, topic: str, payload: dict[str, Any]) -> None:
        channel = self._config.notifications.channels.get(channel_name)
        if channel is None or not channel.enabled:
            return
        if channel.kind is ChannelKind.webpush:
            if not self._webpush_warned:
                self._webpush_warned = True
                await self._safe_emit(
                    "notify.webpush_unavailable",
                    {
                        "channel": channel_name,
                        "message": "web push lands later in M2 — this channel is skipped",
                    },
                )
            return

        counters = self._counters.setdefault(channel_name, ChannelCounters())
        lock = self._locks.setdefault(channel_name, asyncio.Lock())
        prepared = self._absolutize_media(payload)
        try:
            notifier = self._notifiers.get(channel.kind.value)
            if notifier is None:
                raise NotifyError(
                    f"no notifier registered for kind '{channel.kind.value}' — "
                    f"channel '{channel_name}' cannot deliver"
                )
            async with lock:
                await notifier.send(channel, topic, prepared)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # NotifyError or any bug: delivery never crashes the loop
            counters.failed += 1
            streak = self._failure_streaks.get(channel_name, 0) + 1
            self._failure_streaks[channel_name] = streak
            if streak == 1 or streak % _FAILURE_EMIT_EVERY == 0:
                await self._safe_emit(
                    "notify.delivery_failed",
                    {"channel": channel_name, "topic": topic, "error": str(exc)},
                )
            return
        counters.delivered += 1
        self._failure_streaks[channel_name] = 0

    async def _safe_emit(self, topic: str, payload: dict[str, Any]) -> None:
        # The escape hatch must not become an escalation path.
        with contextlib.suppress(Exception):
            await self._emit(topic, payload)

    def _absolutize_media(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Rewrite relative media paths to absolute URLs when a base URL is configured.

        Returns a shallow copy — published payloads are shared across subscribers and must
        never be mutated.
        """
        media = payload.get("media")
        if not self._base_url or not isinstance(media, dict):
            return payload
        rewritten = {
            key: (
                f"{self._base_url}{value}"
                if isinstance(value, str) and value.startswith("/")
                else value
            )
            for key, value in media.items()
        }
        return {**payload, "media": rewritten}
