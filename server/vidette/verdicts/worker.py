"""The verdict worker: a closed incident, the policies for its camera, one VLM verdict each.

    incident.closed ──▶ policies for the camera ──▶ VLM judge (selected backend)
                                                        │
             event.confirmed ◀── any policy alerts ─────┤ (notifications fire here)
             event.dismissed ◀── every policy silent ───┤ (kept, searchable, quiet)
             stays observed  ◀── every policy abstains ─┘ (an incident without a
                                                           verdict never notifies)

Early windows (`incident.updated` with `partial`, a clip folder's `.early/`): the first
seconds of a recording still in progress, judged at once. An early alert confirms the
event right away; anything else leaves it observed. The whole clip is judged when it
closes, after an early alert too, and its verdict replaces the early one:

    early alert ── event.confirmed (the bot alerts) ─▶ clip: alert   ── event.updated
                                                       clip: silent  ── event.dismissed
                                                       clip: no verdict ── event.updated
    early silent ─ stays observed ─────────────────▶ clip judged as usual

What it judges with: the backend chosen on the System page (or the config); with `none`
nothing is judged and incidents stay `observed`. Frames: chosen once from the clip where
the picture changed (vidette.verdicts.frames), else a clip folder's key frames, else
frames from Frigate's recordings over the incident (snapshots when the camera does not
record), else the cached snapshot. `intent.timeline` keeps when each stage happened.

Guards, in the spirit of the prime directives:
- one incident at a time; the VLM's own budget, pace and timeout apply (vidette.vlm);
- only incidents that ended in the last `max_age_s` (15 min) are judged, so a restart, a
  Frigate replay or a clip-folder backfill never turns into a burst of old alerts;
- an event that is no longer `observed` is never judged again, except the whole clip of
  an event an early window confirmed;
- `sensitivity` sets how sure the model must be before an alert counts: relaxed 0.75,
  balanced 0.5, paranoid any; below it the policy counts as silent;
- nothing here can crash the loop: failures become rate-limited `verdict.failed` events.

Calibration (docs/calibration.md, ADR-0020): an alert of a scenario that a step of
calibration made quieter on this camera is still confirmed, with `intent.calibration`
saying so; the bot sends it to the digest. An early window such a step holds back waits
for the whole clip.

The pre-filter (docs/prefilter.md, ADR-0021): with `understanding.triage` on, every
incident is looked at before it waits (an object detector and the clip's own motion
facts) and gets a class, kept on the event as `intent.triage`:

    now    people, moving vehicles, anything half-seen: judged first, newest first
    later  judged only while the model's slot is idle; after `later_max_age` it leaves
           the queue (deep analysis may still take it)
    skip   never judged: nobody there (light change, tiny or the usual movement), or an
           early window with nobody yet; the event stays observed with the reason

Without it every incident is `now`, as before.

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

from vidette.calibration.service import Calibrator
from vidette.core.config import PolicyConfig, Sensitivity, VidetteConfig, VlmBackend, Wake
from vidette.core.events import EventState, InProcessEventBus, Subscription
from vidette.core.incidents import Incident
from vidette.db import Database, EventRow
from vidette.events.payload import canonical_payload
from vidette.ffmpeg import MediaToolError, extract_frame
from vidette.frigate.client import Frame
from vidette.people.check import CropWriter, crop_writer
from vidette.people.report import PeopleReport
from vidette.triage.rules import RANK_LATER, TriageClass, rank, weak_boxes
from vidette.triage.service import Triager
from vidette.verdicts.base import Case, FrameSet, Judge, Verdict, policy_text
from vidette.verdicts.frames import first_video, frame_set

logger = logging.getLogger(__name__)

MAX_AGE_S = 900.0
MIN_CONFIDENCE: dict[Sensitivity, float] = {s: s.min_confidence for s in Sensitivity}
ALBUM_MAX = 3  # frames kept for the alert's album: the subject, never an empty street
ALBUM_FACES = 2  # of those, at most this many best moments of faces
ALBUM_APART_S = 1.0  # two frames closer than this show the same moment
_ROW_WAIT_S = (0.05, 0.1, 0.2, 0.4, 0.8)  # the intake may still be writing the row
_FAILURE_EMIT_EVERY = 10

EmitCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
BackendResolver = Callable[[], Awaitable[VlmBackend]]
JudgeFactory = Callable[[VlmBackend], Judge]
SlotIdle = Callable[[VlmBackend], bool]
WAIT_FOR_SLOT_S = 2.0  # a later clip checks the slot again after this long
WARM_LOOKBACK_S = 86400.0  # the usual movement is rebuilt from this much history


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
    later_judged: int = 0  # judged while the slot was idle (pre-filter class `later`)
    later_expired: int = 0  # waited `later_max_age` without an idle slot
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


def early_may_alert(policy: PolicyConfig, verdict: Verdict) -> bool:
    """An early window's alert goes out now only at a level that wakes for anyone
    (`wake: always`); other levels wait for the whole clip. A policy without levels keeps
    its early alerts."""
    return not policy.levels or policy.wake_for(verdict.label) is Wake.always


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
        calibration: Calibrator | None = None,
        triage: Triager | None = None,
        slot_idle: SlotIdle | None = None,
    ) -> None:
        self._triage = triage
        self._slot_idle = slot_idle
        self._later_max_age_s = (
            config.understanding.triage.later_max_age_s if triage is not None else max_age_s
        )
        self._raw: dict[str, Incident] = {}  # off the bus, not looked at yet
        self._pending: dict[str, Incident] = {}  # looked at, waiting for the judge
        self._ranks: dict[str, int] = {}  # the pre-filter's order of each (triage.rules.rank)
        self._decided: dict[str, int] = {}  # set by `triage`, taken by the intake
        self._ready: asyncio.Event | None = None
        self._people = people
        self._calibration = calibration
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
        self._queued: dict[str, float] = {}  # incident key → when it was taken off the bus
        self.status = WorkerStatus()
        self.backlog = 0  # incidents waiting to be looked at or judged
        self.urgent = 0  # of those, not `later` (deep analysis waits for these)
        self.taken: int | None = None  # the rank `next_incident` took last
        self.working = False  # judging one now

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self._task is not None:
            return
        self._subscription = self._bus.subscribe("incident.*")
        self._task = asyncio.create_task(self._run(self._subscription), name="verdicts")

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
        """Two loops: the intake (off the bus, through the pre-filter) and the judge."""
        self._ready = asyncio.Event()
        if self._triage is not None:
            await self._warm()
        judge = asyncio.create_task(self._judge_loop(), name="verdicts:judge")
        try:
            await self._intake_loop(subscription)
        finally:
            judge.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await judge

    async def _warm(self) -> None:
        """The usual movement per camera from the decisions of the last day."""
        assert self._triage is not None
        try:
            rows = await self._db.list_events(since_ts=self._clock() - WARM_LOOKBACK_S, limit=20000)
            learned = self._triage.warm(rows)
            self._triage.warm_fixtures(rows)
        except Exception:  # a cold memory only means fewer skips for a while
            logger.exception("pre-filter: the usual movement could not be rebuilt")
            return
        if learned:
            logger.info("pre-filter: usual movement learned from %d stored clip(s)", learned)

    async def _intake_loop(self, subscription: Subscription) -> None:
        """Newest first: a burst is looked at from its latest clip back."""
        while True:
            if not self._raw:
                self._take(self._raw, await subscription.get())
            while True:  # everything that arrived meanwhile
                try:
                    self._take(self._raw, subscription.get_nowait())
                except asyncio.QueueEmpty:
                    break
            self._count()
            key = max(self._raw, key=lambda k: self._raw[k].ended_at or self._raw[k].started_at)
            incident = self._raw.pop(key)
            waiting = self._pending.get(incident.key)
            if incident.partial and waiting is not None and not waiting.partial:
                continue  # the whole clip is already waiting
            klass: TriageClass | None = TriageClass.now
            if self._triage is not None:
                try:
                    klass = await self.triage(incident)
                except Exception as exc:  # a failed look never loses a clip
                    logger.exception("pre-filter failed on %s", incident.source_id)
                    await self._failed(f"pre-filter: {type(exc).__name__}: {exc}")
                    klass = TriageClass.now
            order = self._decided.pop(incident.key, None)
            if klass is not None and klass is not TriageClass.skip:
                self._enqueue(incident, rank(klass, None) if order is None else order)
            self._count()

    def _enqueue(self, incident: Incident, order: int) -> None:
        waiting = self._pending.get(incident.key)
        if incident.partial and waiting is not None and not waiting.partial:
            return
        self._pending[incident.key] = incident
        self._ranks[incident.key] = order
        self._count()
        if self._ready is not None:
            self._ready.set()

    def _count(self) -> None:
        later = sum(1 for k in self._pending if self._ranks.get(k, 0) >= RANK_LATER)
        self.backlog = len(self._raw) + len(self._pending)
        self.urgent = self.backlog - later

    async def _judge_loop(self) -> None:
        assert self._ready is not None
        while True:
            incident: Incident | None = None
            try:
                idle = await self._idle() if self._pending else True
                self._ready.clear()
                incident = self.next_incident(self._pending, idle=idle)
                taken = self.taken
                self._count()
                if incident is None:
                    timeout = WAIT_FOR_SLOT_S if self._pending else None
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._ready.wait(), timeout=timeout)
                    continue
                self.working = True
                row = await self.handle(incident)
                if row is not None and taken is not None and taken >= RANK_LATER:
                    self.status.later_judged += 1
            except Exception as exc:  # the loop survives anything
                where = incident.source_id if incident is not None else "the queue"
                logger.exception("verdict worker failed on %s", where)
                await self._failed(f"{type(exc).__name__}: {exc}")
                if incident is None:
                    await asyncio.sleep(WAIT_FOR_SLOT_S)
            finally:
                self.working = False

    async def _idle(self) -> bool:
        """Whether the model's slot is free for a `later` clip (always, without a gate)."""
        if self._slot_idle is None:
            return True
        backend = await self._backend()
        return backend is not VlmBackend.none and self._slot_idle(backend)

    def _best_rank(self, pending: dict[str, Incident]) -> int | None:
        if not pending:
            return None
        return min(self._ranks.get(k, 0) for k in pending)

    def _take(self, pending: dict[str, Incident], message: tuple[str, dict[str, Any]]) -> None:
        """Closed incidents and early windows (partial) wait to be judged. An early window
        never replaces the closed incident of the same clip: the whole clip says more."""
        topic, payload = message
        try:
            incident = Incident.model_validate(payload)
        except ValidationError:
            return
        if topic != "incident.closed" and not incident.partial:
            return
        waiting = pending.get(incident.key)
        if incident.partial and waiting is not None and not waiting.partial:
            return
        self._queued[incident.key] = self._clock()
        pending[incident.key] = incident

    def next_incident(self, pending: dict[str, Incident], *, idle: bool = True) -> Incident | None:
        """Drop what is too old to judge, then take the most recent incident of the best
        rank (triage.rules.rank: people, then moving vehicles, then `later`). A `later`
        one is taken only when the model's slot is `idle`; it waits `later_max_age`, the
        others `max_age_s`."""
        now = self._clock()
        for key, incident in list(pending.items()):
            later = self._ranks.get(key, 0) >= RANK_LATER
            limit = self._later_max_age_s if later else self._max_age_s
            if now - (incident.ended_at or incident.started_at) > limit:
                del pending[key]
                self._ranks.pop(key, None)
                if later:
                    self.status.later_expired += 1
                else:
                    self.status.skipped_old += 1
        best = self._best_rank(pending)
        self.taken = None
        if best is None or (best >= RANK_LATER and not idle):
            return None
        keys = [k for k in pending if self._ranks.get(k, 0) == best]
        key = max(keys, key=lambda k: pending[k].ended_at or pending[k].started_at)
        self._ranks.pop(key, None)
        self.taken = best
        return pending.pop(key)

    def _judgeable(self, incident: Incident, row: EventRow) -> bool:
        """Whether the event still needs this verdict (see `handle`)."""
        phase = (row.intent or {}).get("phase")
        if incident.partial:
            return row.state == EventState.observed.value and phase is None
        early_alert = row.state == EventState.confirmed.value and phase == "early"
        return row.state == EventState.observed.value or early_alert

    async def triage(self, incident: Incident) -> TriageClass | None:
        """Look at one incident before it waits for the judge (vidette.triage), keep the
        decision on its event, and return its class; None when there is nothing to
        judge (no policy for the camera, too old, the event is gone or already
        decided). With the judge switched off nothing is looked at."""
        assert self._triage is not None
        if not policies_for(self._config, incident.camera):
            return None
        if await self._backend() is VlmBackend.none:
            return TriageClass.now  # `handle` leaves it observed, as without a pre-filter
        if self._clock() - (incident.ended_at or incident.started_at) > self._max_age_s:
            self.status.skipped_old += 1
            return None
        row = await self._row(incident)
        if row is None or not self._judgeable(incident, row):
            return None
        case = Case(incident=incident, media=tuple(Path(m) for m in incident.media))
        decision, observation = await self._triage.decide(
            case, camera=incident.camera, partial=incident.partial
        )
        record = decision.record(
            observation,
            phase="early" if incident.partial else "final",
            weak=weak_boxes(observation, self._config.understanding.triage),
        )
        current = await self._db.get_event(row.id)  # the judge may have written since
        intent = dict((current or row).intent or {})
        previous = intent.get("triage")
        klass = decision.klass
        if not incident.partial and isinstance(previous, dict) and previous.get("phase") == "early":
            record["early"] = {k: previous[k] for k in ("class", "why", "reason") if k in previous}
            if previous.get("class") == TriageClass.now.value and klass is not TriageClass.now:
                # someone was in its first seconds: the whole clip is judged like them
                klass = TriageClass.now
                record.update(
                    {"class": klass.value, "why": f"early window: {previous.get('why')}",
                     "reason": "early_" + str(previous.get("reason") or "now")}
                )  # fmt: skip
        summary = None
        if klass is TriageClass.skip and not incident.partial and not intent.get("verdicts"):
            summary = f"not judged: {decision.why}"
        intent["triage"] = record
        self._decided[incident.key] = rank(klass, str(record.get("reason")))
        await self._db.update_event(row.id, intent=intent, summary=summary)
        logger.info(
            "pre-filter %s %s%s: %s (%s) in %.1f s",
            row.id, incident.camera, " early" if incident.partial else "",
            klass.value, record["why"], observation.seconds,
        )  # fmt: skip
        return klass

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

    async def _choose_frames(self, case: Case, event_id: str, count: int) -> FrameSet | None:
        """The frames every policy is judged on, chosen once from the clip (where the
        picture changed, vidette.verdicts.frames) and kept under
        `media_dir/judge-frames/<event id>/` for the alert's album. None without a clip,
        or when the clip cannot be read (each judge then uses what is on disk)."""
        if first_video(case) is None:
            return None
        out_dir = self._media_dir / "judge-frames" / event_id
        try:
            return await frame_set(case, count, work_dir=out_dir, out_dir=out_dir)
        except (MediaToolError, OSError) as exc:
            logger.info("frames for %s not chosen from the clip: %s", event_id, exc)
            return None

    async def handle(self, incident: Incident) -> EventRow | None:
        """Judge one incident; returns the updated row, or None when skipped.

        An early window (`incident.partial`) is judged once, while the event is still
        observed: an alert confirms the event at once (the bot sends it), anything else
        leaves it observed and changes nothing. The closed incident of the same clip is
        then judged whole, also after an early alert: its verdict replaces the early one
        (`event.updated` when the event stays confirmed, `event.dismissed` when the whole
        clip is not an alert), so an early "nothing" never hides a later alert, and an
        early alert is corrected or completed by the clip."""
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
        if row is None:
            return None
        phase = (row.intent or {}).get("phase")
        early_alert = row.state == EventState.confirmed.value and phase == "early"
        if incident.partial:
            if row.state != EventState.observed.value or phase is not None:
                return None
        elif row.state != EventState.observed.value and not early_alert:
            return None

        started = self._clock()
        judge = self._judge(backend)
        frames = self._config.understanding.vlm.frames_per_call
        case = await self._case(incident, row, frames)
        chosen = await self._choose_frames(
            case, f"{row.id}/early" if incident.partial else row.id, frames
        )
        if chosen is not None:
            case = replace(case, frames=chosen)
        people: dict[str, Any] | None = None
        report: PeopleReport | None = None
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
            verdict = await judge.judge(case, policy_text(policy))
            results.append((policy, verdict))

        alerting = [(p, v) for p, v in results if counts_as_alert(p, v)]
        decided = [(p, v) for p, v in results if not v.abstained]
        intent: dict[str, Any] = {
            "judge": getattr(judge, "name", "judge"),
            "backend": backend.value,
            "phase": "early" if incident.partial else "final",
            "verdicts": [verdict_record(policy, verdict) for policy, verdict in results],
        }
        previous = row.intent or {}
        if phase == "early":
            intent["early"] = {
                key: previous[key] for key in ("verdicts", "frames", "stream") if key in previous
            }
        if people is not None:
            intent["people"] = people
        elif "people" in previous:
            intent["people"] = previous["people"]
        if chosen is not None:
            intent["frames"] = [{"path": str(path), "t": at} for path, at in chosen.timed()]
            if chosen.health is not None:
                intent["stream"] = {
                    **chosen.health.model_dump(mode="json"),
                    "interrupted": chosen.health.interrupted,
                }
        album_dir = (
            self._media_dir / "judge-frames" / (f"{row.id}/early" if incident.partial else row.id)
        )
        leading = alerting or decided
        album = await album_frames(
            case, chosen, leading[0][1] if leading else None, report, album_dir
        )
        if album:
            intent["album"] = album
        intent["timeline"] = self._timeline(previous, incident, started, results)
        self.status.judged += 1
        self.status.last_judged_at = self._clock()

        if incident.partial:
            # the first seconds alert only for levels that wake for anyone (ADR-0017): a
            # person who seems to stop in 8 s mostly walks on, and the whole clip says so
            alerting = [(p, v) for p, v in alerting if early_may_alert(p, v)]
            if alerting and await self._calibrated(*alerting[0], incident.camera):
                alerting = []  # a calibration step keeps it out of the alerts for now
            if not alerting:  # nothing yet: the whole clip decides
                await self._db.update_event(row.id, intent=await self._fresh(row.id, intent))
                return await self._db.get_event(row.id)
            policy, verdict = alerting[0]
            self.status.confirmed += 1
            return await self._settle(
                row.id, EventState.confirmed, verdict.rationale, intent, policy.name,
                "event.confirmed",
            )  # fmt: skip
        if alerting:
            policy, verdict = alerting[0]
            self.status.confirmed += 1
            calibrated = (
                None if early_alert else await self._calibrated(policy, verdict, row.camera)
            )
            if calibrated is not None:  # confirmed, but for the digest (ADR-0020)
                intent["calibration"] = calibrated
            topic = "event.updated" if early_alert else "event.confirmed"
            return await self._settle(
                row.id, EventState.confirmed, verdict.rationale, intent, policy.name, topic
            )
        if decided:
            policy, verdict = decided[0]
            self.status.dismissed += 1
            return await self._settle(
                row.id, EventState.dismissed, verdict.rationale, intent, None, "event.dismissed"
            )
        if early_alert:  # the clip got no verdict: the early one stands, with the clip
            kept = dict(previous)
            kept["timeline"] = intent["timeline"]
            kept["phase"] = "final"
            kept["final"] = {"verdicts": intent["verdicts"]}
            return await self._settle(
                row.id, EventState.confirmed, row.summary, kept, row.policy, "event.updated"
            )
        reasons = "; ".join(sorted({v.rationale or "no reason" for _p, v in results}))
        await self._db.update_event(
            row.id, summary=f"no verdict: {reasons}", intent=await self._fresh(row.id, intent)
        )
        self.status.undecided += 1
        return await self._db.get_event(row.id)

    async def _calibrated(
        self, policy: PolicyConfig, verdict: Verdict, camera: str
    ) -> dict[str, Any] | None:
        """The calibration step that sends this alert to the digest, as kept on the event
        (`intent.calibration`); None when it wakes as the policy says."""
        if self._calibration is None:
            return None
        found = await self._calibration.effect(
            policy,
            {"label": verdict.label, "level": verdict.level, "confidence": verdict.confidence},
            camera,
        )
        return None if found is None else found.record()

    def _timeline(
        self,
        previous: dict[str, Any],
        incident: Incident,
        started: float,
        results: list[tuple[PolicyConfig, Verdict]],
    ) -> dict[str, float]:
        """Where the time went: the upstream stages, then the judge's. An early window's
        judge stages are kept under `early_*` next to the clip's."""
        prefix = "early_" if incident.partial else ""
        timeline = {
            k: float(v) for k, v in (previous.get("timeline") or {}).items()
            if isinstance(v, int | float)
        }  # fmt: skip
        for key, value in incident.timeline.items():
            name = f"early_{key}" if incident.partial and key == "picked_up" else key
            timeline.setdefault(name, float(value))
        queued = self._queued.pop(incident.key, None)
        if queued is not None:
            timeline[f"{prefix}judge_queued"] = queued
        timeline[f"{prefix}judge_started"] = started
        timeline[f"{prefix}judge_finished"] = self._clock()
        seen = [v.person_at_s for _p, v in results if v.person_at_s is not None]
        if seen and "person_seen" not in timeline:
            timeline["person_seen"] = incident.started_at + min(seen)
        return {key: round(value, 3) for key, value in timeline.items()}

    async def _fresh(self, event_id: str, intent: dict[str, Any]) -> dict[str, Any]:
        """`intent` with the pre-filter's newest decision on the event: the whole clip
        may have been looked at while its early window was judged."""
        current = await self._db.get_event(event_id)
        triage = (current.intent or {}).get("triage") if current is not None else None
        if triage is not None:
            intent["triage"] = triage
        return intent

    async def _settle(
        self,
        event_id: str,
        state: EventState,
        summary: str | None,
        intent: dict[str, Any],
        policy: str | None,
        topic: str,
    ) -> EventRow | None:
        intent = await self._fresh(event_id, intent)
        await self._db.update_event(
            event_id, state=state.value, summary=summary, intent=intent, policy=policy
        )
        updated = await self._db.get_event(event_id)
        if updated is not None:
            await self._bus.publish(topic, canonical_payload(updated, topic))
        return updated


async def album_frames(
    case: Case,
    chosen: FrameSet | None,
    verdict: Verdict | None,
    report: PeopleReport | None,
    out_dir: Path,
) -> list[dict[str, Any]]:
    """The frames an alert should show: where the subject is, not an empty street.

    First the best moment of each face the check found (the largest face first: that
    frame is where the person is closest and sharpest), extracted from the clip; then
    the judge's own frames that it said show what its verdict is about, clearest first.
    Frames closer than a second to one already picked are skipped. Empty when neither
    is known (no face, an older verdict): the bot then falls back to the judged frames
    from the first one with a person in it."""
    picks: list[dict[str, Any]] = []

    def taken(at: float | None) -> bool:
        return at is not None and any(
            isinstance(p["t"], float | int) and abs(p["t"] - at) < ALBUM_APART_S for p in picks
        )

    video = first_video(case)
    if report is not None and video is not None:
        faces = sorted(
            (s for s in report.seen if s.best_at_s is not None), key=lambda s: -s.face_px
        )
        for seen in faces:
            if len([p for p in picks if p["why"] == "face"]) >= ALBUM_FACES:
                break
            assert seen.best_at_s is not None
            if taken(seen.best_at_s):
                continue
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                path = await extract_frame(video, seen.best_at_s, out_dir / f"face-{seen.n}.jpg")
            except (MediaToolError, OSError) as exc:
                logger.info("no album frame for face %d: %s", seen.n, exc)
                continue
            picks.append({"path": str(path), "t": seen.best_at_s, "why": "face"})
    if verdict is not None and chosen is not None:
        by_time = {at: path for path, at in chosen.timed() if at is not None}
        for at in verdict.subject_at_s:
            frame = by_time.get(at)
            if frame is None or taken(at):
                continue
            picks.append({"path": str(frame), "t": at, "why": "subject"})
    return picks[:ALBUM_MAX]


def verdict_record(policy: PolicyConfig, verdict: Verdict) -> dict[str, Any]:
    """One policy's verdict as kept in the event's `intent.verdicts`."""
    return {
        "policy": policy.name,
        "alert": counts_as_alert(policy, verdict),
        "model_said_alert": verdict.alert,
        "abstained": verdict.abstained,
        "label": verdict.label,
        "level": verdict.level,
        "persons": verdict.persons,
        "person_at_s": verdict.person_at_s,
        "subject_at_s": verdict.subject_at_s,
        "confidence": verdict.confidence,
        "rationale": verdict.rationale,
        "model": verdict.model,
        "latency_s": verdict.latency_s,
        "cost_usd": verdict.cost_usd,
    }
