"""Event model and in-process event bus.

The event model is the contract shared by the API, WebSocket, webhooks and MQTT — see
docs/events-and-automations.md. The bus is deliberately simple: bounded queues, prefix
patterns, drops counted (never silent), single process. It is not a message broker and must
not grow into one (ADR-0001: modular monolith).
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_event_id() -> str:
    """Opaque, lexicographically time-sortable id (millisecond prefix + random suffix)."""
    return f"{int(time.time() * 1000):012x}{uuid4().hex[:10]}"


class EventState(StrEnum):
    observed = "observed"      # candidate, being accumulated from observations
    analyzing = "analyzing"    # promoted, awaiting Tier 3 verdict
    confirmed = "confirmed"    # policy matched → notify
    dismissed = "dismissed"    # policy filtered → kept, searchable, silent


class Observation(BaseModel):
    """A single low-level fact from an adapter or pipeline tier (motion, detection, push)."""

    camera: str
    at: datetime = Field(default_factory=utcnow)
    kind: str  # e.g. "motion", "detection", "track_update", "vendor.doorbell"
    payload: dict[str, Any] = Field(default_factory=dict)


class GeometryFacts(BaseModel):
    """Tier 2 evidence attached to every event — objective, explainable, model-free."""

    approach: float | None = None      # velocity component toward an entry/object zone, 0..1
    dwell_s: float | None = None
    touch: bool = False
    loiter: bool = False
    repeat_pass: int = 0


class IntentVerdict(BaseModel):
    """Tier 3 output — a judgment with provenance, never an anonymous score."""

    label: str  # e.g. "entry_attempt", "delivery", "transit"
    score: float = Field(ge=0.0, le=1.0)
    model: str | None = None
    rationale: str | None = None


class MediaRefs(BaseModel):
    snapshot_path: str | None = None
    clip_path: str | None = None
    preview_path: str | None = None


class Event(BaseModel):
    id: str = Field(default_factory=new_event_id)
    camera: str
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None
    state: EventState = EventState.observed
    kinds: list[str] = Field(default_factory=list)   # person, vehicle, animal, package
    zones: list[str] = Field(default_factory=list)
    geometry: GeometryFacts = Field(default_factory=GeometryFacts)
    summary: str | None = None            # Tier 3 text; None without/before a VLM
    intent: IntentVerdict | None = None
    policy: str | None = None
    media: MediaRefs = Field(default_factory=MediaRefs)
    feedback: Literal["up", "down"] | None = None


def topic_matches(pattern: str, topic: str) -> bool:
    """'event.confirmed' exact; 'event.*' / 'system.*' prefix; '*' everything."""
    if pattern == "*":
        return True
    if pattern.endswith(".*"):
        return topic.startswith(pattern[:-1])
    return topic == pattern


Message = tuple[str, dict[str, Any]]


class Subscription:
    """Async iterator over matching (topic, payload) pairs. Registration is eager —
    messages published after `subscribe()` returns are delivered. Call `close()` to detach.
    """

    def __init__(self, bus: InProcessEventBus, pattern: str, queue: asyncio.Queue[Message]) -> None:
        self._bus = bus
        self.pattern = pattern
        self._queue = queue

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Message:
        return await self._queue.get()

    async def get(self) -> Message:
        return await self._queue.get()

    def close(self) -> None:
        self._bus._detach(self)


class EventBus(Protocol):
    async def publish(self, topic: str, payload: dict[str, Any]) -> None: ...

    def subscribe(self, pattern: str) -> Subscription: ...


class InProcessEventBus:
    """Bounded, drop-oldest, drop-counting pub/sub for a single process."""

    def __init__(self, max_queue_size: int = 1024) -> None:
        self._max_queue_size = max_queue_size
        self._subscriptions: list[Subscription] = []
        self.dropped: int = 0  # surfaced via /metrics (M2); drops are never silent

    def subscribe(self, pattern: str) -> Subscription:
        queue: asyncio.Queue[Message] = asyncio.Queue(self._max_queue_size)
        subscription = Subscription(self, pattern, queue)
        self._subscriptions.append(subscription)
        return subscription

    def _detach(self, subscription: Subscription) -> None:
        if subscription in self._subscriptions:
            self._subscriptions.remove(subscription)

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        for subscription in list(self._subscriptions):
            if not topic_matches(subscription.pattern, topic):
                continue
            queue = subscription._queue
            while True:
                try:
                    queue.put_nowait((topic, payload))
                    break
                except asyncio.QueueFull:
                    self.dropped += 1
                    queue.get_nowait()  # drop the oldest: fresh events beat stale ones
