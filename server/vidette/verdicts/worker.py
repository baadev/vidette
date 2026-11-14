"""The verdict worker: a closed incident, the policies for its camera, one VLM verdict each.

    incident.closed ──▶ policies for the camera ──▶ VLM judge (selected backend)
                                                        │
             event.confirmed ◀── any policy alerts ─────┤ (notifications fire here)
             event.dismissed ◀── every policy silent ───┤ (kept, searchable, quiet)
             stays observed  ◀── every policy abstains ─┘ (an incident without a
                                                           verdict never notifies)

What it judges with: the backend chosen on the System page (or the config); with `none`
nothing is judged and incidents stay `observed`. Frames: a clip folder's key frames, else
frames from Frigate's recordings over the incident (snapshots when the camera does not
record), else the cached snapshot.

Guards, in the spirit of the prime directives:
- one incident at a time; the VLM's own budget, pace and timeout apply (vidette.vlm);
- only incidents that ended in the last `max_age_s` (15 min) are judged, so a restart, a
  Frigate replay or a clip-folder backfill never turns into a burst of old alerts;
- an event that is no longer `observed` is never judged again;
- `sensitivity` sets how sure the model must be before an alert counts: relaxed 0.75,
  balanced 0.5, paranoid any; below it the policy counts as silent;
- nothing here can crash the loop: failures become rate-limited `verdict.failed` events.

Known people (docs/people.md): with a face check configured, the incident's frames go
through it once before the policies are judged. The report rides along to the judge
(names for a local model only) and is kept in the event's `intent.people`, with face
crops under `media_dir/faces/<event id>/` for the web app's confirm buttons. A failed
face check never blocks the verdict: it is recorded and the judge runs without it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from vidette.core.config import PolicyConfig, Sensitivity, VidetteConfig, VlmBackend
from vidette.core.events import EventState, InProcessEventBus, Subscription
from vidette.core.incidents import Incident
from vidette.db import Database, EventRow
from vidette.events.payload import canonical_payload
from vidette.frigate.client import Frame
from vidette.people.check import CropWriter, crop_writer
from vidette.people.report import PeopleReport
from vidette.verdicts.base import Case, Judge, PolicyText, Verdict

logger = logging.getLogger(__name__)

MAX_AGE_S = 900.0
MIN_CONFIDENCE: dict[Sensitivity, float] = {
    Sensitivity.relaxed: 0.75,
    Sensitivity.balanced: 0.5,
    Sensitivity.paranoid: 0.0,
}
_ROW_WAIT_S = (0.05, 0.1, 0.2, 0.4, 0.8)  # the intake may still be writing the row
_FAILURE_EMIT_EVERY = 10

EmitCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
BackendResolver = Callable[[], Awaitable[VlmBackend]]
JudgeFactory = Callable[[VlmBackend], Judge]


class FaceCheck(Protocol):
    """The slice of PeopleCheck the worker uses."""

    async def check(self, case: Case, crops: CropWriter | None = None) -> PeopleReport: ...


class FrameSource(Protocol):
    """The slice of FrigateClient the worker uses (frames over an incident)."""

    async def incident_frames(
        self, incident: Incident, *, count: int = 4, height: int | None = 480
    ) -> list[Frame]: ...


@dataclass
class WorkerStatus:
    judged: int = 0
    confirmed: int = 0
    dismissed: int = 0
    undecided: int = 0  # every policy abstained: stays observed
    skipped_old: int = 0
    failed: int = 0
    last_judged_at: float | None = None
    last_error: str | None = None


def policies_for(config: VidetteConfig, camera: str) -> list[PolicyConfig]:
    return [p for p in config.policies if p.cameras == "all" or camera in p.cameras]


def counts_as_alert(policy: PolicyConfig, verdict: Verdict) -> bool:
    if not verdict.alert or verdict.abstained:
        return False
    threshold = MIN_CONFIDENCE[policy.sensitivity]
    return verdict.confidence is None or verdict.confidence >= threshold


class VerdictWorker:
    def __init__(
        self,
        config: VidetteConfig,
        bus: InProcessEventBus,
        db: Database,
        *,
        emit: EmitCallback,
        backend: BackendResolver,
        judges: JudgeFactory,
        frames: FrameSource | None = None,  # FrigateClient when Frigate is configured
        media_dir: Path,
        max_age_s: float = MAX_AGE_S,
        clock: Callable[[], float] = time.time,
        people: FaceCheck | None = None,
    ) -> None:
        self._people = people
        self._config = config
        self._bus = bus
        self._db = db
        self._emit = emit
        self._backend = backend
        self._judges = judges
        self._frames = frames
        self._media_dir = media_dir
        self._max_age_s = max_age_s
        self._clock = clock
        self._subscription: Subscription | None = None
        self._task: asyncio.Task[None] | None = None
        self._judge_cache: dict[VlmBackend, Judge] = {}
        self.status = WorkerStatus()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self._task is not None:
            return
        self._subscription = self._bus.subscribe("incident.closed")
        self._task = asyncio.create_task(self._pump(self._subscription), name="verdicts")

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
            _topic, payload = await subscription.get()
            try:
                incident = Incident.model_validate(payload)
            except ValidationError:
                continue
            try:
                await self.handle(incident)
            except Exception as exc:  # the loop survives anything
                logger.exception("verdict worker failed on %s", payload.get("source_id"))
                await self._failed(f"{type(exc).__name__}: {exc}")

    async def _failed(self, error: str) -> None:
        self.status.failed += 1
        self.status.last_error = error[:300]
        if self.status.failed == 1 or self.status.failed % _FAILURE_EMIT_EVERY == 0:
            with contextlib.suppress(Exception):
                await self._emit(
                    "verdict.failed", {"error": error[:300], "count": self.status.failed}
                )

    def _judge(self, backend: VlmBackend) -> Judge:
        if backend not in self._judge_cache:
            self._judge_cache[backend] = self._judges(backend)
        return self._judge_cache[backend]

    async def _row(self, incident: Incident) -> EventRow | None:
        for delay in (0.0, *_ROW_WAIT_S):
            if delay:
                await asyncio.sleep(delay)
            row = await self._db.get_event_by_source(incident.source, incident.source_id)
            if row is not None:
                return row
        return None

    async def _case(self, incident: Incident, row: EventRow, frames: int) -> Case:
        if incident.media:
            return Case(incident=incident, media=tuple(Path(m) for m in incident.media))
        if self._frames is not None:
            fetched = await self._frames.incident_frames(incident, count=frames)
            if fetched:
                folder = self._media_dir / "verdict-frames" / row.id
                folder.mkdir(parents=True, exist_ok=True)
                paths = []
                for index, frame in enumerate(fetched, start=1):
                    path = folder / f"{index}.jpg"
                    await asyncio.to_thread(path.write_bytes, frame.jpeg)
                    paths.append(path)
                return Case(incident=incident, media=tuple(paths))
        if row.snapshot_path is not None:
            return Case(incident=incident, media=(Path(row.snapshot_path),))
        return Case(incident=incident)

    async def handle(self, incident: Incident) -> EventRow | None:
        """Judge one closed incident; returns the updated row, or None when skipped."""
        policies = policies_for(self._config, incident.camera)
        if not policies:
            return None
        backend = await self._backend()
        if backend is VlmBackend.none:
            return None
        ended = incident.ended_at or incident.started_at
        if self._clock() - ended > self._max_age_s:
            self.status.skipped_old += 1
            return None
        row = await self._row(incident)
        if row is None or row.state != EventState.observed.value:
            return None

        judge = self._judge(backend)
        frames = self._config.understanding.vlm.frames_per_call
        case = await self._case(incident, row, frames)
        people: dict[str, Any] | None = None
        if self._people is not None:
            try:
                report = await self._people.check(
                    case, crop_writer(self._media_dir, f"faces/{row.id}")
                )
            except Exception as exc:  # a face check never blocks the verdict
                logger.warning("face check failed on %s: %s", row.id, exc)
                people = {"error": f"{type(exc).__name__}: {exc}"[:300]}
            else:
                case = replace(case, people=report)
                people = report.model_dump(mode="json")
        results: list[tuple[PolicyConfig, Verdict]] = []
        for policy in policies:
            text = PolicyText(policy.name, policy.description, policy.known_people)
            verdict = await judge.judge(case, text)
            results.append((policy, verdict))

        alerting = [(p, v) for p, v in results if counts_as_alert(p, v)]
        decided = [(p, v) for p, v in results if not v.abstained]
        intent = {
            "judge": getattr(judge, "name", "judge"),
            "backend": backend.value,
            "verdicts": [
                {
                    "policy": policy.name,
                    "alert": counts_as_alert(policy, verdict),
                    "model_said_alert": verdict.alert,
                    "abstained": verdict.abstained,
                    "label": verdict.label,
                    "confidence": verdict.confidence,
                    "rationale": verdict.rationale,
                    "model": verdict.model,
                    "latency_s": verdict.latency_s,
                    "cost_usd": verdict.cost_usd,
                }
                for policy, verdict in results
            ],
        }
        if people is not None:
            intent["people"] = people
        self.status.judged += 1
        self.status.last_judged_at = self._clock()
        if alerting:
            policy, verdict = alerting[0]
            state, summary, policy_name = EventState.confirmed, verdict.rationale, policy.name
            self.status.confirmed += 1
        elif decided:
            policy, verdict = decided[0]
            state, summary, policy_name = EventState.dismissed, verdict.rationale, None
            self.status.dismissed += 1
        else:
            reasons = "; ".join(sorted({v.rationale or "no reason" for _p, v in results}))
            await self._db.update_event(row.id, summary=f"no verdict: {reasons}", intent=intent)
            self.status.undecided += 1
            return await self._db.get_event(row.id)

        await self._db.update_event(
            row.id, state=state.value, summary=summary, intent=intent, policy=policy_name
        )
        updated = await self._db.get_event(row.id)
        if updated is not None:
            topic = f"event.{state.value}"
            await self._bus.publish(topic, canonical_payload(updated, topic))
        return updated
