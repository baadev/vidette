"""MQTT bridge with Home Assistant discovery.

Publishes Vidette events to an MQTT broker under ``<topic_prefix>/…`` and, when discovery is
enabled, announces one person-occupancy binary sensor per camera via Home Assistant MQTT
discovery. The topic contract lives in docs/events-and-automations.md:

- ``<prefix>/status``          retained ``online``/``offline`` availability (offline is the LWT);
- ``<prefix>/<camera>/event``  the canonical event JSON, on ``event.confirmed``;
- ``<prefix>/<camera>/person`` ``ON``/``OFF`` occupancy derived from confirmed/ended events;
- ``<prefix>/system/event``    every ``system.*`` payload (disk pressure, camera offline, …).

Deliberately omitted: raw per-frame motion topics. Publishing motion-level chatter to MQTT
would flood brokers and automations with noise the cascade exists to filter — *events* are
the signal (see docs/events-and-automations.md). Occupancy flips on confirmed events only.

Design constraints mirror the notification dispatcher (CLAUDE.md prime directives): this
bridge can never crash or stall anything upstream. Connection failures become rate-limited
``mqtt.connection_failed`` bus events (first, then every 5th consecutive) plus an exponential
1→30 s reconnect backoff; a failed publish is counted and logged, never fatal to the loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Protocol

import aiomqtt

from vidette.core.config import MqttConfig, VidetteConfig
from vidette.core.events import InProcessEventBus, Subscription

logger = logging.getLogger(__name__)

_FAILURE_EMIT_EVERY = 5  # emit on the 1st consecutive connection failure, then every 5th
_DISCOVERY_PREFIX = "homeassistant"


class MqttClient(Protocol):
    """The slice of ``aiomqtt.Client`` the publisher uses — the seam tests fake."""

    async def __aenter__(self) -> object: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None: ...

    async def publish(
        self, topic: str, payload: str | bytes | None = None, qos: int = 0, retain: bool = False
    ) -> None: ...


@dataclass
class MqttStatus:
    connected: bool
    published_total: int
    failed_total: int


def _dumps(payload: dict[str, Any]) -> str:
    """Serialize for MQTT; ``default=str`` keeps a stray datetime from killing a publish."""
    return json.dumps(payload, default=str)


class MqttPublisher:
    """Bridges the in-process event bus to an MQTT broker (with HA discovery).

    ``start()`` is a no-op unless ``integrations.mqtt.enabled``; ``stop()`` is idempotent.
    """

    def __init__(
        self,
        config: VidetteConfig,
        bus: InProcessEventBus,
        *,
        emit: Callable[[str, dict[str, Any]], Awaitable[None]],
        client_factory: Callable[[], MqttClient] | None = None,
        reconnect_initial: float = 1.0,
        reconnect_max: float = 30.0,
    ) -> None:
        self._config = config
        self._mqtt: MqttConfig = config.integrations.mqtt
        self._bus = bus
        self._emit = emit
        self._client_factory = (
            client_factory if client_factory is not None else self._aiomqtt_client
        )
        self._reconnect_initial = reconnect_initial
        self._reconnect_max = reconnect_max
        self._task: asyncio.Task[None] | None = None
        self._subscriptions: list[Subscription] = []
        self._client: MqttClient | None = None
        self._connected = False
        self._published = 0
        self._failed = 0
        self._failure_streak = 0

    # --- lifecycle -----------------------------------------------------------------------------

    def start(self) -> None:
        if not self._mqtt.enabled:
            logger.debug("mqtt: integrations.mqtt.enabled is false — publisher not started")
            return
        if self._task is not None:
            return
        self._subscriptions = [self._bus.subscribe("event.*"), self._bus.subscribe("system.*")]
        self._task = asyncio.create_task(self._supervise(), name="mqtt:supervisor")

    async def stop(self) -> None:
        """Best-effort offline announce, then tear down. Safe to call more than once."""
        task, self._task = self._task, None
        client = self._client
        if self._connected and client is not None:
            with contextlib.suppress(Exception):
                await client.publish(self._status_topic, "offline", qos=0, retain=True)
                self._published += 1
        for subscription in self._subscriptions:
            subscription.close()
        self._subscriptions.clear()
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._connected = False
        self._client = None

    def status(self) -> MqttStatus:
        return MqttStatus(
            connected=self._connected,
            published_total=self._published,
            failed_total=self._failed,
        )

    # --- connection supervision ------------------------------------------------------------------

    def _aiomqtt_client(self) -> MqttClient:
        """Default factory: a real aiomqtt client with a retained ``offline`` LWT."""
        mqtt = self._mqtt
        if mqtt.host is None:  # config validation enforces host whenever mqtt is enabled
            raise ValueError("integrations.mqtt.host is required when mqtt is enabled")
        return aiomqtt.Client(
            hostname=mqtt.host,
            port=mqtt.port,
            username=mqtt.username,
            password=mqtt.password,
            will=aiomqtt.Will(self._status_topic, "offline", qos=0, retain=True),
        )

    async def _supervise(self) -> None:
        """Connect → announce → pump; on any connection error, back off and try again."""
        backoff = self._reconnect_initial
        while True:
            client = self._client_factory()
            try:
                async with client:
                    self._client = client
                    self._connected = True
                    self._failure_streak = 0
                    backoff = self._reconnect_initial
                    await self._announce(client)
                    await self._pump(client)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # connection-level failure: reconnect, never crash
                self._failure_streak += 1
                streak = self._failure_streak
                if streak == 1 or streak % _FAILURE_EMIT_EVERY == 0:
                    await self._safe_emit(
                        "mqtt.connection_failed", self._failure_payload(exc, backoff)
                    )
                logger.warning(
                    "mqtt: connection to %s:%s failed (%s) — retrying in %.1fs",
                    self._mqtt.host,
                    self._mqtt.port,
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._reconnect_max)
            finally:
                self._connected = False
                self._client = None

    async def _announce(self, client: MqttClient) -> None:
        """Retained availability + (optionally) one HA discovery config per camera."""
        await self._publish(client, self._status_topic, "online", retain=True)
        if not self._mqtt.discovery:
            return
        prefix = self._mqtt.topic_prefix
        for camera_id, camera in self._config.cameras.items():
            display_name = camera.name or camera_id
            object_id = f"{prefix}_{camera_id}_person"
            discovery = {
                "name": f"{display_name} person",
                "unique_id": object_id,
                "state_topic": f"{prefix}/{camera_id}/person",
                "device_class": "occupancy",
                "payload_on": "ON",
                "payload_off": "OFF",
                "availability_topic": self._status_topic,
                "device": {
                    "identifiers": [f"{prefix}_{camera_id}"],
                    "name": display_name,
                    "manufacturer": "Vidette",
                },
            }
            await self._publish(
                client,
                f"{_DISCOVERY_PREFIX}/binary_sensor/{object_id}/config",
                _dumps(discovery),
                retain=True,
            )

    # --- bus consumption --------------------------------------------------------------------------

    async def _pump(self, client: MqttClient) -> None:
        """Drain all bus subscriptions concurrently for the lifetime of one connection."""
        consumers = [
            asyncio.create_task(self._consume(client, sub), name=f"mqtt:{sub.pattern}")
            for sub in self._subscriptions
        ]
        try:
            await asyncio.gather(*consumers)
        finally:
            for consumer in consumers:
                consumer.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)

    async def _consume(self, client: MqttClient, subscription: Subscription) -> None:
        while True:
            topic, payload = await subscription.get()
            await self._handle(client, topic, payload)

    async def _handle(self, client: MqttClient, topic: str, payload: dict[str, Any]) -> None:
        prefix = self._mqtt.topic_prefix
        if topic.startswith("system."):
            await self._publish(client, f"{prefix}/system/event", _dumps(payload))
            return
        camera = payload.get("camera")
        if not isinstance(camera, str) or not camera:
            logger.warning("mqtt: %s payload carries no camera id — skipped", topic)
            return
        if topic == "event.confirmed":
            await self._publish(client, f"{prefix}/{camera}/event", _dumps(payload))
            kinds = payload.get("kinds")
            if isinstance(kinds, list) and "person" in kinds:
                await self._publish(client, f"{prefix}/{camera}/person", "ON")
        elif topic == "event.ended":
            await self._publish(client, f"{prefix}/{camera}/person", "OFF")
        # Other event.* stages (enriched, dismissed) stay off MQTT: automations key on
        # confirmed/ended, and dismissed events are deliberately silent everywhere.

    async def _publish(
        self, client: MqttClient, topic: str, payload: str, *, retain: bool = False, qos: int = 0
    ) -> None:
        """One publish. ``MqttError`` propagates (broken connection → reconnect); any other
        failure is counted and logged — a bad message must never kill the pump."""
        try:
            await client.publish(topic, payload, qos=qos, retain=retain)
        except asyncio.CancelledError:
            raise
        except aiomqtt.MqttError:
            self._failed += 1
            raise
        except Exception:
            self._failed += 1
            logger.exception("mqtt: publish to %s failed — message dropped", topic)
        else:
            self._published += 1

    # --- helpers ---------------------------------------------------------------------------------

    @property
    def _status_topic(self) -> str:
        return f"{self._mqtt.topic_prefix}/status"

    def _failure_payload(self, exc: Exception, backoff: float) -> dict[str, Any]:
        host, port = self._mqtt.host, self._mqtt.port
        return {
            "host": host,
            "port": port,
            "attempt": self._failure_streak,
            "error": str(exc),
            "retry_in_s": backoff,
            "message": (
                f"cannot reach MQTT broker at {host}:{port} ({exc}) — check that the broker "
                f"is running and reachable and that integrations.mqtt host/port/credentials "
                f"are correct; retrying in {backoff:.0f}s"
            ),
        }

    async def _safe_emit(self, topic: str, payload: dict[str, Any]) -> None:
        # The escape hatch must not become an escalation path.
        with contextlib.suppress(Exception):
            await self._emit(topic, payload)
