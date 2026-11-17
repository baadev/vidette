"""The incident as `vidette serve` keeps it (ADR-0022): on an alert, incident mode and the
links to the other cameras, stored on the events.

    event.confirmed ─▶ for_alert(event)  (also called by the bot, before its text: the
        │                                 same work, done once per alert)
        ├─ incident mode: incident-mode.json until now + keep_for (an alert a calibration
        │  step sends to the digest does not start one)
        ├─ links (vidette.incident.link): faces, clothes on neighbouring cameras, time
        └─ stored: the alert's `intent.incident` {id, role: alert, links}; each linked
           event's {id, role: linked, alert, evidence, …}; an incident an event already
           belongs to (an earlier alert minutes ago) keeps its id. A linked event the
           judge had dismissed is marked `reopened` when the alert wakes for anyone: the
           web page and the digest show it as part of the incident.

While incident mode lasts, the verdict worker asks `active` (the pre-filter never skips
a clip then) and `context` (one line for the judge about the alert). Failures are logged
and never stop an alert.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from vidette.core.config import VidetteConfig, Wake
from vidette.core.events import InProcessEventBus, Subscription
from vidette.db import Database, EventRow
from vidette.ffmpeg import MediaToolError
from vidette.incident.link import Link, find_links, moment
from vidette.incident.look import Look, look_of
from vidette.incident.mode import IncidentMode, incident_path, read_incident, write_incident
from vidette.telegram.routing import alerting_verdict
from vidette.triage.detector import ObjectDetector
from vidette.triage.track import Sighting, person_track, stored_sightings

logger = logging.getLogger(__name__)

LINK_TIMEOUT_S = 60.0
LOOK_TRACK_EVERY_S = 2.0  # an event without stored sightings: the detector this often


@dataclass(frozen=True)
class IncidentRecord:
    id: str
    alert: str
    links: list[Link]

    @property
    def same_person(self) -> list[Link]:
        return [link for link in self.links if link.same_person]


def incident_of(row: EventRow) -> dict[str, Any] | None:
    found = (row.intent or {}).get("incident")
    return dict(found) if isinstance(found, Mapping) else None


class IncidentService:
    def __init__(
        self,
        config: VidetteConfig,
        db: Database,
        bus: InProcessEventBus,
        *,
        detector: ObjectDetector | None = None,
        clock: Callable[[], float] = time.time,
        work_dir: Path | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.bus = bus
        self.detector = detector
        self._clock = clock
        self._work_dir = work_dir
        settings = config.incident
        self.mode_path = settings.mode_file or incident_path(config.storage.database)
        self._work: dict[str, asyncio.Future[IncidentRecord | None]] = {}
        self._subscription: Subscription | None = None
        self._task: asyncio.Task[None] | None = None
        self._looks: dict[str, Look | None] = {}

    # --- lifecycle ---------------------------------------------------------------------

    def start(self) -> None:
        if self._task is not None or not self.config.incident.enabled:
            return
        self._subscription = self.bus.subscribe("event.confirmed")
        self._task = asyncio.create_task(self._run(self._subscription), name="incidents")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if self._subscription is not None:
            self._subscription.close()
            self._subscription = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self, subscription: Subscription) -> None:
        while True:
            _topic, payload = await subscription.get()
            event_id = payload.get("id")
            if not isinstance(event_id, str):
                continue
            try:
                row = await self.db.get_event(event_id)
                if row is not None:
                    await self.for_alert(row)
            except Exception:  # the loop survives anything
                logger.exception("incident for %s failed", event_id)

    # --- incident mode -------------------------------------------------------------------

    def active(self, at: float | None = None) -> IncidentMode | None:
        """The incident mode in force at `at` (default now), if any."""
        mode = read_incident(self.mode_path)
        when = self._clock() if at is None else at
        if mode is None or not (mode.since - 60 <= when < mode.until):
            return None
        return mode

    def context(self, camera: str, at: float) -> str | None:
        """One line for the judge during incident mode: what the alert was. Neutral on
        purpose: the frames still decide."""
        mode = self.active(at)
        if mode is None:
            return None
        minutes = max(0, round((at - mode.since) / 60))
        when = datetime.fromtimestamp(mode.since).strftime("%H:%M")
        what = f" ({mode.label})" if mode.label else ""
        where = "this camera" if mode.camera == camera else f"camera {mode.camera}"
        return (
            f"Context: about {minutes} min before this clip, at {when}, {where} alerted{what}."
            " Judge only what these frames show."
        )

    def _start_mode(self, row: EventRow) -> None:
        keep = self.config.incident.seconds("keep_for")
        intent = row.intent or {}
        if keep <= 0 or intent.get("calibration") is not None:
            return
        verdict = alerting_verdict(row.intent, row.policy)
        now = self._clock()
        mode = write_incident(
            self.mode_path,
            IncidentMode(
                since=min(now, row.started_at), until=now + keep, event_id=row.id,
                camera=row.camera, level=verdict.get("level"), label=verdict.get("label"),
            ),
            now=now,
        )  # fmt: skip
        logger.info(
            "incident mode until %s (alert %s on %s): every camera keeps what it sees",
            datetime.fromtimestamp(mode.until).strftime("%H:%M:%S"), row.id, row.camera,
        )  # fmt: skip

    # --- the links ---------------------------------------------------------------------------

    async def for_alert(self, row: EventRow) -> IncidentRecord | None:
        """The alert's incident, worked out once (callers share the work)."""
        if not self.config.incident.enabled:
            return None
        future = self._work.get(row.id)
        if future is None:
            future = asyncio.ensure_future(self._build(row))
            self._work[row.id] = future
            for old in list(self._work)[:-64]:  # remember the last few alerts only
                if self._work[old].done():
                    del self._work[old]
        return await asyncio.shield(future)

    async def _build(self, row: EventRow) -> IncidentRecord | None:
        try:
            self._start_mode(row)
        except OSError as exc:
            logger.warning("incident mode not started: %s", exc)
        try:
            return await asyncio.wait_for(self._link(row), timeout=LINK_TIMEOUT_S)
        except Exception as exc:  # never in the way of an alert
            logger.warning("incident links for %s failed: %s", row.id, exc)
            return None

    async def _link(self, row: EventRow) -> IncidentRecord | None:
        within = self.config.incident.seconds("link_within")
        lag = max((self.config.cross_camera.lag_s(f.camera) for f in self.config.clip_folders),
                  default=0.0)  # fmt: skip
        since = moment(row) - within - lag - 600  # a long clip started before its person
        rows = [
            r for r in await self.db.list_events(since_ts=since, limit=500)
            if r.started_at <= (row.ended_at or row.started_at) + within + lag
        ]  # fmt: skip
        look = self._look if self.config.incident.look else None
        links = await find_links(
            row, rows, self.config, sightings_for=self._sightings, look_for=look
        )
        current = await self.db.get_event(row.id) or row
        existing = incident_of(current)
        incident_id = None
        for link in links:
            linked = await self.db.get_event(link.event_id)
            found = incident_of(linked) if linked is not None else None
            if found is not None and link.same_person:
                incident_id = str(found.get("id"))
        if existing is not None:
            incident_id = str(existing.get("id"))
        incident_id = incident_id or f"inc-{row.id}"
        verdict = alerting_verdict(row.intent, row.policy)
        policy = next((p for p in self.config.policies if p.name == row.policy), None)
        wakes_all = policy is not None and policy.wake_for(verdict.get("label")) is Wake.always
        record = IncidentRecord(incident_id, row.id, links)
        await self._store(current, record, verdict, wakes_all)
        strong = len(record.same_person)
        logger.info(
            "incident %s: alert %s on %s, %d linked event(s) on other cameras (%d the same "
            "person by face or clothes)", incident_id, row.id, row.camera, len(links), strong,
        )  # fmt: skip
        return record

    async def _store(
        self,
        alert: EventRow,
        record: IncidentRecord,
        verdict: Mapping[str, Any],
        wakes_all: bool,
    ) -> None:
        intent = dict(alert.intent or {})
        intent["incident"] = {
            "id": record.id,
            "role": "alert",
            "at": round(self._clock(), 3),
            "links": [link.record() for link in record.links],
        }
        await self.db.update_event(alert.id, intent=intent)
        for link in record.links:
            linked = await self.db.get_event(link.event_id)
            if linked is None:
                continue
            mine = dict(linked.intent or {})
            previous = mine.get("incident")
            if isinstance(previous, Mapping) and previous.get("role") == "alert":
                continue  # an alert of its own: it keeps its own record
            mine["incident"] = {
                "id": record.id,
                "role": "linked",
                "alert": alert.id,
                "alert_camera": alert.camera,
                "alert_at": round(moment(alert), 2),
                "alert_label": verdict.get("label"),
                "evidence": link.evidence,
                "score": link.score,
                "reopened": wakes_all and link.same_person and linked.state == "dismissed",
            }
            await self.db.update_event(linked.id, intent=mine)

    async def _sightings(self, row: EventRow) -> list[Sighting]:
        found = stored_sightings(row.intent)
        if found or self.detector is None or not row.clip_path:
            return found
        clip = Path(row.clip_path)
        if not clip.is_file():
            return []
        try:
            return await person_track(
                clip, self.detector, every_s=LOOK_TRACK_EVERY_S, max_frames=60,
                work_dir=self._work_dir,
            )  # fmt: skip
        except (MediaToolError, OSError) as exc:
            logger.info("no person track for %s: %s", row.id, exc)
            return []

    async def _look(self, row: EventRow, sightings: list[Sighting]) -> Look | None:
        if row.id in self._looks:
            return self._looks[row.id]
        found: Look | None = None
        if row.clip_path and Path(row.clip_path).is_file() and sightings:
            found = await look_of(Path(row.clip_path), sightings, work_dir=self._work_dir)
        self._looks[row.id] = found
        if len(self._looks) > 64:
            self._looks.pop(next(iter(self._looks)))
        return found
