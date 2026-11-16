"""The deep-analysis worker inside `vidette serve` (ADR-0019).

    events of the last `lookback` ──▶ deep_jobs (sqlite, one row per event) ──▶ one at a time
         with people, vehicles,          priority: alert, level, unsure,         │
         alerts, or never judged         faces, person, vehicle, unjudged        ▼
                                                                 look + decide (analyst)
                                                                          │
     events.deep ◀── the verdict, live vs deep (compare) ◀────────────────┘
     labels.yaml ◀── disputed / uncertain clips as review drafts (label_disputes)

When it runs: always one request at a time, through the process's shared slot
(vidette.vlm.gate). Inside `window` (01:00-07:00 by default) it runs back to back but
only while the live judge rests: it waits while the live judge or the assistant use the
slot, and a live call that arrives cancels the deep request (the job goes back to the
queue, the frames already described are kept for the retry). Outside the window it takes
one clip every `trickle_every` as a turn of its own, in line with live calls and not
cancelled, so a live judge that never rests (a busy day) waits for one deep request now
and then instead of starving it; `outside_window: off` leaves the day to the live judge.
Before every request it asks the server's `/slots` and sends only when `min_idle_slots`
are idle; otherwise it backs off, 15 s doubling to 5 min, so it never takes another
tenant's slot. At most `max_clips_per_day` clips are analysed per local day.

Why inside serve and not a separate agent: the one slot is shared with the live judge
and the assistant, and only one process can see all three and let live calls go first;
the event database has one writer (ADR-0008); and the digest that quotes the deep
descriptions lives here too. A failure here never reaches the live path: every job is
caught, retried up to `max_attempts`, and reported as a rate-limited system event.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from vidette.clipfolder import clip_incident
from vidette.core.config import DeepConfig, PolicyConfig, VidetteConfig
from vidette.core.incidents import Incident
from vidette.db import Database, DeepJobRow, EventRow
from vidette.deep.analyst import DeepAnalyst, DeepError, DeepResult, Look
from vidette.deep.compare import REVIEW, compare, live_side, worst
from vidette.deep.compare import Side as VerdictSide
from vidette.label.feedback import FeedbackError, LiveVerdict, propose
from vidette.people.report import PeopleReport
from vidette.verdicts.base import Case, policy_text
from vidette.verdicts.worker import policies_for, verdict_record
from vidette.vlm.endpoint import VlmEndpoint
from vidette.vlm.gate import SlotGate, slot_state, was_preempted

logger = logging.getLogger(__name__)

EmitCallback = Callable[[str, dict[str, Any]], Awaitable[None]]

SETTLE_S = 1200.0  # an event this fresh may still get its live verdict
REFILL_EVERY_S = 600.0
IDLE_SLEEP_S = 300.0
BACKOFF_S = (15.0, 300.0)  # first wait, longest wait for a free slot
_FAILURE_EMIT_EVERY = 10

# priority (lower first) and reason
P_ALERT, P_LEVEL, P_UNSURE, P_FACES, P_PERSON, P_VEHICLE, P_UNJUDGED = range(7)


@dataclass
class DeepStatus:
    analysed: int = 0
    disputed: int = 0
    uncertain: int = 0
    failed: int = 0
    preempted: int = 0
    backoffs: int = 0
    slot_checks: int = 0
    labelled: int = 0
    last_at: float | None = None
    last_error: str | None = None
    waiting_for: str | None = None  # why it is idle now, in a few words
    slots: deque[tuple[float, int, int]] = field(default_factory=lambda: deque(maxlen=200))


def classify(row: EventRow, *, include_unjudged: bool) -> tuple[int, str] | None:
    """Whether an event is worth a deep look, and how urgent: (priority, reason)."""
    intent = row.intent or {}
    verdicts = [v for v in intent.get("verdicts") or [] if isinstance(v, dict)]
    people = intent.get("people") if isinstance(intent.get("people"), dict) else {}
    faces = bool((people or {}).get("seen"))
    persons = max((v.get("persons") or 0 for v in verdicts if isinstance(v.get("persons"), int)),
                  default=0)  # fmt: skip
    if row.state == "confirmed":
        return P_ALERT, "alert"
    if any(v.get("model_said_alert") or v.get("alert") for v in verdicts):
        return P_LEVEL, "alert level below the bar"
    labels = {str(v.get("label") or "") for v in verdicts}
    if verdicts and all(v.get("abstained") for v in verdicts):
        if persons or faces:
            return P_UNSURE, "live judge unsure"
        return (P_UNJUDGED, "live judge unsure") if include_unjudged else None
    if faces:
        return P_FACES, "faces"
    if persons:
        return P_PERSON, "person"
    if any(label.startswith(("car_", "vehicle")) for label in labels):
        return P_VEHICLE, "vehicle"
    if not verdicts and include_unjudged:
        return P_UNJUDGED, "never judged"
    return None


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts).date().isoformat()


class DeepWorker:
    def __init__(
        self,
        config: VidetteConfig,
        db: Database,
        analyst: DeepAnalyst,
        gate: SlotGate,
        *,
        media_dir: Path,
        emit: EmitCallback,
        endpoint: VlmEndpoint,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        live_busy: Callable[[], bool] = lambda: False,
    ) -> None:
        self.config = config
        self.deep: DeepConfig = config.understanding.deep
        self.db = db
        self.analyst = analyst
        self.gate = gate
        self.media_dir = media_dir
        self._emit = emit
        self.endpoint = endpoint
        self._http = http or httpx.AsyncClient()
        self._owns_http = http is None
        self._clock = clock
        self._sleep = sleep
        self._live_busy = live_busy  # incidents waiting for the live judge
        self._task: asyncio.Task[None] | None = None
        self._last_refill = 0.0
        self._last_started = 0.0
        self._partial: dict[str, list[Look]] = {}  # looks of a preempted job, for its retry
        self._preemptible = True  # night: yield to live calls; day trickle: a turn of its own
        self.status = DeepStatus()

    # --- lifecycle -------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self._task is None and self.deep.enabled:
            self._task = asyncio.create_task(self._loop(), name="deep")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._owns_http:
            await self._http.aclose()

    async def _loop(self) -> None:
        requeued = await self.db.deep_requeue_running()
        if requeued:
            logger.info("deep: %d job(s) left running by the last run are queued again", requeued)
        while True:
            try:
                wait = await self.step()
            except Exception as exc:  # the loop survives anything
                logger.exception("deep analysis step failed")
                await self._failed(f"{type(exc).__name__}: {exc}")
                wait = IDLE_SLEEP_S
            if wait > 0:
                await self._sleep(wait)

    async def _failed(self, error: str) -> None:
        self.status.failed += 1
        self.status.last_error = error[:300]
        if self.status.failed == 1 or self.status.failed % _FAILURE_EMIT_EVERY == 0:
            with contextlib.suppress(Exception):
                await self._emit("deep.failed", {"error": error[:300], "count": self.status.failed})

    # --- scheduling ------------------------------------------------------------------------

    async def refill(self, *, force: bool = False) -> int:
        """Queue the events of the last `lookback` worth a deep look (not queued before)."""
        now = self._clock()
        if not force and now - self._last_refill < REFILL_EVERY_S:
            return 0
        self._last_refill = now
        since = now - self.deep.lookback_s
        known = await self.db.deep_known(since)
        rows = await self.db.list_events(since_ts=since, limit=20000)
        jobs = []
        for row in rows:
            if row.id in known or row.deep is not None:
                continue
            if now - (row.ended_at or row.started_at) < SETTLE_S:
                continue  # the live judge may still decide
            found = classify(row, include_unjudged=self.deep.include_unjudged)
            if found is not None and policies_for(self.config, row.camera):
                jobs.append((row.id, row.camera, row.started_at, found[0], found[1]))
        added = await self.db.deep_enqueue(jobs)
        if added:
            logger.info("deep: %d event(s) queued", added)
        return added

    def in_window(self) -> bool:
        local = datetime.fromtimestamp(self._clock())
        return self.deep.in_window(local.hour * 60 + local.minute)

    async def allowed_in(self) -> float:
        """Seconds until the next clip may start (0: now), from the cap, the window and
        the trickle pace."""
        now = self._clock()
        local = datetime.fromtimestamp(now)
        midnight_next = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() + 86400
        counts = await self.db.deep_counts(day=_day(now))
        used = counts.get("done", 0) + counts.get("failed", 0)
        if used >= self.deep.max_clips_per_day:
            self.status.waiting_for = f"daily cap {used}/{self.deep.max_clips_per_day}"
            return max(60.0, midnight_next - now + 1)
        minute = local.hour * 60 + local.minute
        if self.deep.in_window(minute):
            return 0.0
        if self.deep.outside_window == "off":
            start, _end = self.deep.window_minutes
            until = (start - minute) % 1440
            self.status.waiting_for = f"window {self.deep.window}"
            return max(60.0, until * 60.0 - local.second)
        since = now - self._last_started
        if since < self.deep.trickle_every_s:
            self.status.waiting_for = f"trickle every {self.deep.trickle_every}"
            return self.deep.trickle_every_s - since
        return 0.0

    async def step(self) -> float:
        """One turn of the loop: refill, then run one job if allowed; how long to sleep."""
        await self.refill()
        wait = await self.allowed_in()
        if wait > 0:
            return min(wait, IDLE_SLEEP_S)
        night = self.in_window()
        if night and (self._live_busy() or self.gate.busy or self.gate.live_waiting):
            self.status.waiting_for = "the live judge"
            return 5.0
        now = self._clock()
        job = await self.db.deep_next(
            since_ts=now - self.deep.lookback_s, max_attempts=self.deep.max_attempts
        )
        if job is None:
            self.status.waiting_for = "nothing queued"
            return IDLE_SLEEP_S
        self.status.waiting_for = None
        self._last_started = now
        await self.run_job(job, preemptible=night)
        return 0.0

    # --- one job ---------------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def _slot(self) -> AsyncIterator[None]:
        """The shared slot, and then a free slot on the server (ADR-0019)."""
        async with self.gate.background(preemptible=self._preemptible):
            delay = BACKOFF_S[0]
            while self.deep.slots == "check":
                state = await slot_state(self.endpoint, self._http)
                if state is None:
                    break  # the server does not say: the request finds out
                self.status.slots.append((self._clock(), state.busy, state.total))
                self.status.slot_checks += 1
                if state.idle >= self.deep.min_idle_slots:
                    break
                self.status.backoffs += 1
                self.status.waiting_for = f"server busy ({state.busy}/{state.total} slots)"
                await self._sleep(delay)
                delay = min(delay * 2, BACKOFF_S[1])
            yield

    def _case(self, row: EventRow) -> Case | None:
        clip = Path(row.clip_path) if row.clip_path else None
        if clip is None or not clip.is_file():
            return None
        incident: Incident | None = None
        for folder in self.config.clip_folders:
            if clip.resolve().is_relative_to(folder.path.resolve()):
                with contextlib.suppress(OSError, ValueError):
                    incident = clip_incident(folder, clip)
                break
        if incident is None:
            incident = Incident(
                source=row.source or "vidette",
                source_id=row.source_ref or row.id,
                camera=row.camera,
                started_at=row.started_at,
                ended_at=row.ended_at,
                media=[str(clip)],
            )
        people = None
        raw = (row.intent or {}).get("people")
        if isinstance(raw, dict) and "seen" in raw:
            with contextlib.suppress(ValueError):
                people = PeopleReport.model_validate(raw)
        return Case(incident=incident, media=tuple(Path(m) for m in incident.media), people=people)

    async def run_job(self, job: DeepJobRow, *, preemptible: bool = True) -> None:
        row = await self.db.get_event(job.event_id)
        now = self._clock()
        if row is None:
            await self.db.deep_update(
                job.event_id, state="skipped", error="event gone", done_at=now
            )
            return
        case = self._case(row)
        policies = policies_for(self.config, row.camera)
        if case is None or not policies:
            why = "no clip on disk" if case is None else "no policy for the camera"
            await self.db.deep_update(job.event_id, state="skipped", error=why, done_at=now)
            return
        await self.db.deep_update(job.event_id, state="running", taken_at=now)
        self._preemptible = preemptible
        work = self.media_dir / "deep-frames" / row.id
        started = self._clock()
        stats = dict(job.stats)
        checks = self.status.slot_checks

        async def analyse() -> DeepResult:
            return await self.analyst.analyse(
                case, [policy_text(p) for p in policies], work_dir=work, slot=self._slot,
                resume=self._partial.get(row.id),
            )  # fmt: skip

        task: asyncio.Task[DeepResult] = asyncio.create_task(analyse(), name=f"deep:{row.id}")
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            await self.db.deep_update(job.event_id, state="queued", taken_at=None)
            raise
        if was_preempted(task):
            self.status.preempted += 1
            stats["preempted"] = int(stats.get("preempted", 0)) + 1
            partial = getattr(self.analyst, "last_looks", None)
            if isinstance(partial, list) and partial:
                self._partial[row.id] = partial
            await self.db.deep_update(job.event_id, state="queued", taken_at=None, stats=stats)
            logger.info("deep %s: a live call took the slot back; queued again", row.id)
            return
        error = None if task.cancelled() else task.exception()
        if task.cancelled() or error is not None:
            retry = isinstance(error, DeepError) and error.retry
            attempts = job.attempts + 1
            state = "queued" if retry and attempts < self.deep.max_attempts else "failed"
            message = "cancelled" if error is None else f"{type(error).__name__}: {error}"
            stats["attempt_seconds"] = round(self._clock() - started, 1)
            await self.db.deep_update(
                job.event_id, state=state, attempts=attempts, error=message[:300],
                done_at=self._clock() if state == "failed" else None,
                day=_day(self._clock()) if state == "failed" else None, stats=stats,
            )  # fmt: skip
            logger.info("deep %s: %s (%s)", row.id, message[:200], state)
            if state == "failed" or not isinstance(error, DeepError):
                await self._failed(message)
            return
        result = task.result()
        self._partial.pop(row.id, None)
        seconds = round(self._clock() - started, 1)
        record = await self._record(row, policies, result, seconds)
        await self.db.set_event_deep(row.id, record)
        stats.update(
            {
                "seconds": seconds,
                "calls": [
                    {"kind": c.kind, "s": c.seconds, "in": c.tokens_in, "out": c.tokens_out,
                     "images": c.images}
                    for c in result.calls
                ],
                "frames": len(result.frames.paths),
                "agreement": record["agreement"],
                "mode": "night" if preemptible else "trickle",
            }
        )  # fmt: skip
        if self.status.slot_checks > checks and self.status.slots:
            _at, busy, total = self.status.slots[-1]
            stats["slots"] = f"{busy}/{total}"  # the server when the last request went
        done = self._clock()
        await self.db.deep_update(
            job.event_id, state="done", done_at=done, day=_day(done), error=None, stats=stats
        )
        self.status.analysed += 1
        self.status.last_at = done
        if record["agreement"] == "disputed":
            self.status.disputed += 1
        elif record["agreement"] == "uncertain":
            self.status.uncertain += 1
        first = record["verdicts"][0] if record["verdicts"] else {}
        logger.info(
            "deep %s %s: %s/%s %s in %.0f s, %d call(s), slots %s; %s",
            row.id, row.camera, first.get("label"), first.get("level"), record["agreement"],
            seconds, len(result.calls), stats.get("slots", "?"), record["why"],
        )  # fmt: skip

    async def _record(
        self, row: EventRow, policies: list[PolicyConfig], result: DeepResult, seconds: float
    ) -> dict[str, Any]:
        verdicts = []
        per_policy = []
        categories = []
        for policy, (_text, verdict) in zip(policies, result.verdicts, strict=True):
            stored = verdict_record(policy, verdict)
            verdicts.append(stored)
            live = live_side(row.intent, policy.name)
            category, why = compare(
                live, VerdictSide.of(stored), min_confidence=self.deep.min_confidence
            )
            categories.append(category)
            per_policy.append({"policy": policy.name, "agreement": category, "why": why,
                               "live": live.scenario, "live_level": live.level})  # fmt: skip
        overall = worst(categories)
        lead = next(
            (v for v, p in zip(verdicts, per_policy, strict=True) if p["agreement"] == overall),
            verdicts[0] if verdicts else {},
        )
        record: dict[str, Any] = {
            "v": 1,
            "at": round(self._clock(), 3),
            "model": result.model,
            "backend": self.endpoint.backend.value,
            "frames": [{"path": str(path), "t": at} for path, at in result.frames.timed()],
            "looks": [
                {
                    "frames": f"{k.first}-{k.last}",
                    "observations": k.observations,
                    "persons": k.persons,
                    "vehicles": k.vehicles,
                    "subject": k.subject,
                }
                for k in result.looks
            ],
            "persons": result.persons,
            "vehicles": result.vehicles,
            "verdicts": verdicts,
            "description": lead.get("rationale"),
            "agreement": overall,
            "why": next((p["why"] for p in per_policy if p["agreement"] == overall), ""),
            "policies": per_policy,
            "seconds": seconds,
        }
        if overall in REVIEW and self.deep.label_disputes and row.clip_path:
            record["review"] = await self._propose(row, policies, lead, per_policy)
        return record

    async def _propose(
        self,
        row: EventRow,
        policies: list[PolicyConfig],
        lead: dict[str, Any],
        per_policy: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """A disputed or uncertain clip becomes a draft in its dataset's labels.yaml, with
        the deep verdict as the proposal and both judges in its notes."""
        policy = next((p for p in policies if p.name == lead.get("policy")), policies[0])
        scenarios = {sid: s.description for sid, s in policy.scenario_configs().items()}
        said = next((p for p in per_policy if p["policy"] == policy.name), per_policy[0])
        note = (
            f"deep {said['agreement']}: live {said.get('live') or 'none'}"
            f" ({said.get('live_level') or '-'}), deep {lead.get('label') or 'unsure'}"
            f" ({lead.get('level') or '-'})"
        )
        description = f"[{lead.get('label') or '?'} vs live {said.get('live') or '-'}] " + str(
            lead.get("rationale") or ""
        )
        verdict = LiveVerdict(
            by=f"deep vlm-{self.endpoint.backend.value} {lead.get('model') or ''}".strip(),
            scenario=lead.get("label") if not lead.get("abstained") else None,
            alert=bool(lead.get("alert")),
            description=" ".join(description.split())[:200],
            confidence=lead.get("confidence"),
            at=datetime.now().astimezone().replace(microsecond=0),
        )
        assert row.clip_path is not None
        try:
            found = await asyncio.to_thread(
                propose, Path(row.clip_path), verdict, scenarios=scenarios, note=note
            )
        except FeedbackError as exc:
            logger.info("deep %s: no review draft (%s)", row.id, exc)
            return {"error": str(exc)[:200]}
        if found is None:
            return {"skipped": "already reviewed"}
        self.status.labelled += 1
        dataset, number = found
        return {"dataset": dataset, "number": number}


__all__ = ["DeepStatus", "DeepWorker", "classify"]
