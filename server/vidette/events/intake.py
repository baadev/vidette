"""Event intake: incidents from the bus become rows in the event store.

One row per upstream incident (keyed by `source` + `source_ref`, e.g. a Frigate review
id), in state `observed`: Vidette has seen it and has no verdict yet. Every phase updates
the same row (objects, zones, end time, the upstream's own summary), caches one snapshot
from the upstream (best effort, a few attempts, since Frigate may not have one at the very
start), and publishes `event.observed` so open Events pages refresh.

`event.observed` never notifies: the notification dispatcher drops it by rule, and the
MQTT bridge only publishes confirmed/ended events. From week 2 of the v0.2 plan the
verdict worker moves rows to `confirmed` or `dismissed`, and only then can a phone buzz.

Sources that already hold local media (clip folders, ADR-0010) put it on the incident:
the clip becomes the event's clip and a key frame its snapshot, copied into `media_dir`.

Crash containment as everywhere: `handle()` never raises; a failed snapshot or a bad
payload is logged and the next message is processed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from vidette.core.events import EventState, InProcessEventBus, Subscription, new_event_id
from vidette.core.incidents import Incident
from vidette.db import Database, EventRow
from vidette.events.payload import canonical_payload

logger = logging.getLogger(__name__)

SNAPSHOT_MAX_ATTEMPTS = 3
OBSERVED_TOPIC = "event.observed"
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")


class SnapshotSource(Protocol):
    """The slice of FrigateClient the intake needs."""

    async def event_snapshot(
        self, event_id: str, *, crop: bool = False, height: int | None = None
    ) -> bytes: ...


def upstream_facts(incident: Incident, previous: dict[str, Any] | None) -> dict[str, Any]:
    """What the upstream said, merged so a later phase never erases an earlier summary."""
    summary = (
        incident.upstream_summary.model_dump(mode="json")
        if incident.upstream_summary is not None
        else (previous or {}).get("summary")
    )
    return {
        "severity": incident.severity,
        "detections": list(incident.detections),
        "sub_labels": list(incident.sub_labels),
        "summary": summary,
    }


class EventIntake:
    def __init__(
        self,
        db: Database,
        bus: InProcessEventBus,
        *,
        media_dir: Path,
        snapshots: SnapshotSource | None = None,
    ) -> None:
        self._db = db
        self._bus = bus
        self._media_dir = media_dir
        self._snapshots = snapshots
        self._snapshot_attempts: dict[str, int] = {}
        self._subscription: Subscription | None = None
        self._task: asyncio.Task[None] | None = None
        self.observed_total = 0

    def start(self) -> None:
        """Subscribe now (eagerly: nothing published after start() is missed) and pump."""
        if self._task is not None:
            return
        self._subscription = self._bus.subscribe("incident.*")
        self._task = asyncio.create_task(self._pump(self._subscription), name="events:intake")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if self._subscription is not None:
            self._subscription.close()
            self._subscription = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _pump(self, subscription: Subscription) -> None:
        while True:
            topic, payload = await subscription.get()
            await self.handle(topic, payload)

    async def handle(self, topic: str, payload: dict[str, Any]) -> EventRow | None:
        """Upsert the event for one incident message. Never raises."""
        try:
            incident = Incident.model_validate(payload)
        except ValidationError as exc:
            logger.warning("event intake: %s payload is not an incident (%s)", topic, exc)
            return None
        try:
            row = await self._upsert(incident)
            if row is None:
                return None
            row = await self._maybe_snapshot(row, incident) or row
            await self._bus.publish(OBSERVED_TOPIC, canonical_payload(row, OBSERVED_TOPIC))
            return row
        except Exception:
            logger.exception("event intake failed for %s %s; continuing", topic, incident.key)
            return None

    async def _upsert(self, incident: Incident) -> EventRow | None:
        existing = await self._db.get_event_by_source(incident.source, incident.source_id)
        if existing is None:
            event_id = new_event_id()
            try:
                await self._db.insert_event(
                    event_id,
                    incident.camera,
                    incident.started_at,
                    EventState.observed.value,
                    list(incident.objects),
                    list(incident.zones),
                    {},
                    ended_at=incident.ended_at,
                    source=incident.source,
                    source_ref=incident.source_id,
                    upstream=upstream_facts(incident, None),
                    clip_path=_local_clip(incident),
                )
                self.observed_total += 1
                return await self._db.get_event(event_id)
            except ValueError:
                existing = await self._db.get_event_by_source(
                    incident.source, incident.source_id
                )
                if existing is None:
                    raise
        await self._db.update_event(
            existing.id,
            kinds=list(incident.objects) or None,
            zones=list(incident.zones) or None,
            ended_at=incident.ended_at,
            upstream=upstream_facts(incident, existing.upstream),
        )
        return await self._db.get_event(existing.id)

    async def _maybe_snapshot(self, row: EventRow, incident: Incident) -> EventRow | None:
        if row.snapshot_path is None and (frame := _local_frame(incident)) is not None:
            return await self._store_snapshot(row, await asyncio.to_thread(frame.read_bytes))
        if (
            self._snapshots is None
            or row.snapshot_path is not None
            or not incident.detections
            or self._snapshot_attempts.get(row.id, 0) >= SNAPSHOT_MAX_ATTEMPTS
        ):
            return None
        self._snapshot_attempts[row.id] = self._snapshot_attempts.get(row.id, 0) + 1
        try:
            jpeg = await self._snapshots.event_snapshot(incident.detections[0])
        except Exception as exc:
            logger.info(
                "event intake: no snapshot yet for %s (attempt %d/%d): %s",
                incident.key,
                self._snapshot_attempts[row.id],
                SNAPSHOT_MAX_ATTEMPTS,
                exc,
            )
            return None
        self._snapshot_attempts.pop(row.id, None)
        return await self._store_snapshot(row, jpeg)

    async def _store_snapshot(self, row: EventRow, jpeg: bytes) -> EventRow | None:
        camera_dir = row.camera if _SAFE_SEGMENT.match(row.camera) else "unknown-camera"
        path = self._media_dir / camera_dir / "events" / row.id / "snapshot.jpeg"
        await asyncio.to_thread(_write_file, path, jpeg)
        await self._db.update_event(row.id, snapshot_path=str(path))
        return await self._db.get_event(row.id)


_VIDEO = (".mp4", ".mov", ".mkv")
_IMAGE = (".jpg", ".jpeg")


def _local_clip(incident: Incident) -> str | None:
    return next((m for m in incident.media if m.lower().endswith(_VIDEO)), None)


def _local_frame(incident: Incident) -> Path | None:
    """The middle key frame: the first one is often the camera still waking up."""
    frames = [m for m in incident.media if m.lower().endswith(_IMAGE)]
    if not frames:
        return None
    path = Path(frames[len(frames) // 2])
    return path if path.is_file() else None


def _write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".part")
    partial.write_bytes(data)
    partial.replace(path)
