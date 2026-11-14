"""Frigate as an incident source: MQTT in, typed incidents on the event bus out.

Subscribes to Frigate's broker (docs/frigate.md):

- ``<prefix>/reviews``   review items → ``incident.opened|updated|closed|summarized``
- ``<prefix>/events``    tracked objects → ``object.opened|updated|closed`` (optional)
- ``<prefix>/available`` Frigate liveness → ``system.frigate.available|unavailable``

Bus payloads are `Incident` / `TrackedObject` dumps (vidette.core.incidents). The phase
is Vidette's, not Frigate's message type: with a severity filter of ``[alert]``, a review
that starts as a ``detection`` and is upgraded later is *opened* on the upgrade. ``end``
always maps to *closed* and ``genai`` to *summarized*; consumers upsert by
``Incident.key``, so a message for an incident they missed (Vidette restarted
mid-review) is still usable.

Same containment rules as every other worker (CLAUDE.md prime directives): nothing here
may crash or stall anything else. A malformed payload is counted, logged and reported as a
rate-limited ``frigate.payload_invalid`` system event; a lost broker connection becomes a
rate-limited ``frigate.mqtt_connection_failed`` event plus a 1 to 30 s backoff.

`handle()` is public so recorded sessions can be replayed without a broker (tests,
`vidette frigate replay`), and `raw_sink` lets `vidette frigate tail --record` capture
fresh fixtures from a live Frigate.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol

import aiomqtt
from pydantic import ValidationError

from vidette.core.config import FrigateConfig
from vidette.core.events import InProcessEventBus
from vidette.core.incidents import Phase, as_payload, incident_topic, object_topic
from vidette.frigate.models import (
    FrigateEventMessage,
    FrigateReviewMessage,
    object_phase,
    to_incident,
    to_tracked_object,
)

logger = logging.getLogger(__name__)

_CONNECT_FAILURE_EMIT_EVERY = 5  # 1st consecutive failure, then every 5th
_INVALID_EMIT_EVERY = 20  # 1st invalid payload, then every 20th
_SEEN_CAP = 10_000  # open incidents remembered (end messages can get lost)
_ERROR_CHARS = 300


class InboundMessage(Protocol):
    @property
    def topic(self) -> object: ...

    @property
    def payload(self) -> object: ...


class MqttSubscriber(Protocol):
    """The slice of ``aiomqtt.Client`` the source uses: the seam tests fake."""

    async def __aenter__(self) -> object: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None: ...

    async def subscribe(self, topic: str, qos: int = 0) -> object: ...

    @property
    def messages(self) -> AsyncIterator[Any]: ...


RawSink = Callable[[str, bytes], None]
Emit = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class FrigateSourceStatus:
    connected: bool
    frigate_available: bool | None  # None until Frigate's availability topic was seen
    messages_total: int
    invalid_total: int
    incidents_total: int
    last_message_at: float | None


def topic_text(topic: object) -> str:
    """aiomqtt gives a `Topic` (with `.value`); fakes and recordings give a str."""
    value = getattr(topic, "value", topic)
    return value if isinstance(value, str) else str(value)


def payload_bytes(payload: object) -> bytes:
    if isinstance(payload, bytes | bytearray):
        return bytes(payload)
    if payload is None:
        return b""
    return str(payload).encode("utf-8")


class FrigateSource:
    """Turns Frigate's MQTT messages into incidents on the bus. `start()`/`stop()` own a
    supervisor task; `handle()` processes one message and never raises."""

    def __init__(
        self,
        config: FrigateConfig,
        bus: InProcessEventBus,
        *,
        emit: Emit,
        client_factory: Callable[[], MqttSubscriber] | None = None,
        raw_sink: RawSink | None = None,
        clock: Callable[[], float] = time.time,
        reconnect_initial: float = 1.0,
        reconnect_max: float = 30.0,
    ) -> None:
        self._config = config
        self._bus = bus
        self._emit = emit
        self._client_factory = client_factory or self._aiomqtt_client
        self._raw_sink = raw_sink
        self._clock = clock
        self._reconnect_initial = reconnect_initial
        self._reconnect_max = reconnect_max
        self._prefix = config.mqtt.topic_prefix.rstrip("/")
        self._cameras = None if config.cameras == "all" else set(config.cameras)
        self._severities = {severity.value for severity in config.severities}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._task: asyncio.Task[None] | None = None
        self._connected = False
        self._available: bool | None = None
        self._messages = 0
        self._invalid = 0
        self._incidents = 0
        self._last_message_at: float | None = None
        self._failure_streak = 0

    # --- lifecycle -------------------------------------------------------------------------------

    @property
    def topics(self) -> list[str]:
        names = ["available", "reviews"]
        if self._config.tracked_objects:
            names.append("events")
        return [f"{self._prefix}/{name}" for name in names]

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._supervise(), name="frigate:source")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._connected = False

    def status(self) -> FrigateSourceStatus:
        return FrigateSourceStatus(
            connected=self._connected,
            frigate_available=self._available,
            messages_total=self._messages,
            invalid_total=self._invalid,
            incidents_total=self._incidents,
            last_message_at=self._last_message_at,
        )

    # --- connection ------------------------------------------------------------------------------

    def _aiomqtt_client(self) -> MqttSubscriber:
        mqtt = self._config.mqtt
        return aiomqtt.Client(
            hostname=mqtt.host,
            port=mqtt.port,
            username=mqtt.username,
            password=mqtt.password,
            identifier=f"vidette-{int(self._clock())}",
        )

    async def _supervise(self) -> None:
        backoff = self._reconnect_initial
        while True:
            client = self._client_factory()
            try:
                async with client:
                    for topic in self.topics:
                        await client.subscribe(topic, qos=1)
                    self._connected = True
                    self._failure_streak = 0
                    backoff = self._reconnect_initial
                    async for message in client.messages:
                        await self.handle(topic_text(message.topic), message.payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # broker gone, auth refused, …: reconnect, never crash
                self._failure_streak += 1
                streak = self._failure_streak
                if streak == 1 or streak % _CONNECT_FAILURE_EMIT_EVERY == 0:
                    await self._safe_emit(
                        "frigate.mqtt_connection_failed", self._failure_payload(exc, backoff)
                    )
                logger.warning(
                    "frigate source: MQTT %s:%s failed (%s), retrying in %.1fs",
                    self._config.mqtt.host,
                    self._config.mqtt.port,
                    exc,
                    backoff,
                )
            finally:
                self._connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._reconnect_max)

    # --- messages --------------------------------------------------------------------------------

    async def handle(self, topic: str, payload: object) -> None:
        """Process one MQTT message. Never raises."""
        raw = payload_bytes(payload)
        self._messages += 1
        self._last_message_at = self._clock()
        if self._raw_sink is not None:
            try:
                self._raw_sink(topic, raw)
            except Exception:
                logger.exception("frigate source: raw sink failed for %s", topic)
        name = topic.removeprefix(f"{self._prefix}/")
        try:
            if name == "available":
                await self._on_available(raw.decode("utf-8", errors="replace").strip())
            elif name == "reviews":
                await self._on_review(FrigateReviewMessage.model_validate_json(raw))
            elif name == "events" and self._config.tracked_objects:
                await self._on_object(FrigateEventMessage.model_validate_json(raw))
        except ValidationError as exc:
            await self._invalid_payload(topic, exc)
        except Exception:
            logger.exception("frigate source: failed to handle %s; continuing", topic)

    def _wanted_camera(self, camera: str) -> bool:
        return self._cameras is None or camera in self._cameras

    async def _on_review(self, message: FrigateReviewMessage) -> None:
        review = message.after
        if not self._wanted_camera(review.camera) or review.severity not in self._severities:
            return
        incident = to_incident(review)
        if message.type == "end":
            phase = Phase.closed
            self._seen.pop(review.id, None)
        elif message.type == "genai":
            phase = Phase.summarized
        elif message.type in ("new", "update"):
            phase = Phase.updated if review.id in self._seen else Phase.opened
            self._remember(review.id)
        else:
            logger.debug("frigate source: ignoring review message type %r", message.type)
            return
        if phase is Phase.opened:
            self._incidents += 1
        await self._bus.publish(incident_topic(phase), as_payload(incident))

    async def _on_object(self, message: FrigateEventMessage) -> None:
        obj = message.after
        phase = object_phase(message.type)
        if phase is None or obj.false_positive or not self._wanted_camera(obj.camera):
            return
        await self._bus.publish(object_topic(phase), as_payload(to_tracked_object(obj)))

    async def _on_available(self, state: str) -> None:
        available = state == "online"
        if available == self._available:
            return
        first = self._available is None
        self._available = available
        if first and available:
            return  # the normal case at startup is not news
        kind = "frigate.available" if available else "frigate.unavailable"
        await self._safe_emit(
            kind,
            {
                "state": state,
                "message": (
                    "Frigate is back online"
                    if available
                    else f"Frigate reports '{state}' on its availability topic: no incidents "
                    "arrive until it is back"
                ),
            },
        )

    def _remember(self, review_id: str) -> None:
        self._seen[review_id] = None
        self._seen.move_to_end(review_id)
        while len(self._seen) > _SEEN_CAP:
            self._seen.popitem(last=False)

    async def _invalid_payload(self, topic: str, exc: ValidationError) -> None:
        self._invalid += 1
        detail = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc']) or 'payload'}: {issue['msg']}"
            for issue in exc.errors()[:3]
        )[:_ERROR_CHARS]
        logger.warning("frigate source: invalid payload on %s (%s)", topic, detail)
        if self._invalid == 1 or self._invalid % _INVALID_EMIT_EVERY == 0:
            await self._safe_emit(
                "frigate.payload_invalid",
                {
                    "topic": topic,
                    "count": self._invalid,
                    "error": detail,
                    "message": (
                        f"a message on {topic} did not match the Frigate payload Vidette "
                        "expects; check that Frigate is 0.14 or newer (docs/frigate.md) and "
                        "record a sample with `vidette frigate tail --record` for a bug report"
                    ),
                },
            )

    # --- helpers ---------------------------------------------------------------------------------

    def _failure_payload(self, exc: Exception, backoff: float) -> dict[str, Any]:
        host, port = self._config.mqtt.host, self._config.mqtt.port
        return {
            "host": host,
            "port": port,
            "attempt": self._failure_streak,
            "error": str(exc)[:_ERROR_CHARS],
            "retry_in_s": backoff,
            "message": (
                f"cannot read Frigate's MQTT broker at {host}:{port} ({exc}); check that the "
                "broker runs, that frigate.mqtt host/port/credentials match Frigate's own "
                f"mqtt section, and that Vidette can reach it; retrying in {backoff:.0f}s"
            ),
        }

    async def _safe_emit(self, kind: str, payload: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            await self._emit(kind, payload)


# --- recordings (fixtures and `vidette frigate tail --record`) ---------------------------------


def record_line(topic: str, raw: bytes, *, at: float) -> str:
    """One JSON line per MQTT message; JSON payloads stay readable, others become text."""
    text = raw.decode("utf-8", errors="replace")
    try:
        payload: object = json.loads(text)
    except ValueError:
        payload = text
    return json.dumps({"at": at, "topic": topic, "payload": payload}, ensure_ascii=False)


def iter_recording(path: Path) -> Iterator[tuple[str, bytes]]:
    """Yield (topic, raw payload) from a recording written with `record_line`."""
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                topic = str(entry["topic"])
                payload = entry["payload"]
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"{path}:{number}: not a recorded MQTT message ({exc})") from exc
            raw = payload.encode("utf-8") if isinstance(payload, str) else json.dumps(payload)
            yield topic, raw if isinstance(raw, bytes) else raw.encode("utf-8")


async def replay(source: FrigateSource, path: Path) -> int:
    """Feed a recorded session through `source.handle`; returns the message count."""
    count = 0
    for topic, raw in iter_recording(path):
        await source.handle(topic, raw)
        count += 1
    return count
