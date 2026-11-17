"""`vidette triage replay`: the pre-filter over the events a running service already has.

Every event of the last days whose clip is still on disk is looked at again
(vidette.triage.observe: the sidecar, the activity map, the detector), classified with
the current rules, and compared with what is known about it:

- what the live judge and deep analysis said (scenario, level, how many people);
- the alerts the bot sent and the 👍/👎 they got;
- reviewed labels in the clip folders' `labels.yaml`.

The database is opened read-only and no model is called; the detector runs on this
machine. Observations are cached in a JSONL file (`--cache`), one line per event, so a
second run with other thresholds costs nothing. The report says how many clips a day
each class would get, which known alerts and people it would have skipped (the number
that must be 0), and what share of clips the live judge could then reach, from a
simulation of its queue with the measured time per verdict.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from vidette.clipfolder import clip_incident
from vidette.collect.clips import load_meta
from vidette.core.config import TriageConfig, VidetteConfig, Wake
from vidette.core.incidents import Incident
from vidette.db import EventRow
from vidette.triage.detector import ObjectDetector
from vidette.triage.observe import (
    OBSERVATION_VERSION,
    Observation,
    look_densely,
    observe,
    wants_dense,
)
from vidette.triage.rules import (
    FIXTURE_KEEP,
    Fixtures,
    TriageClass,
    UsualMovement,
    classify,
    rank,
    teaches,
    weak_boxes,
)
from vidette.verdicts.base import Case

logger = logging.getLogger(__name__)

Say = Callable[[str], None]


def case_for_event(config: VidetteConfig, row: EventRow) -> Case | None:
    """The case a stored event stands for: its clip and, for a clip folder, the key
    frames next to it. None when the clip is gone."""
    clip = Path(row.clip_path) if row.clip_path else None
    if clip is None or not clip.is_file():
        return None
    incident: Incident | None = None
    for folder in config.clip_folders:
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
    return Case(incident=incident, media=tuple(Path(m) for m in incident.media))


def load_cache(path: Path) -> dict[str, Observation]:
    found: dict[str, Observation] = {}
    if not path.is_file():
        return found
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            data = json.loads(line)
            observation = Observation.model_validate(data["obs"])
        except (ValueError, KeyError, TypeError, ValidationError):
            continue
        if observation.v == OBSERVATION_VERSION:
            found[str(data["event"])] = observation
    return found


async def gather(
    rows: Iterable[EventRow],
    config: VidetteConfig,
    detectors: list[ObjectDetector],
    *,
    cache: Path,
    frames: int = 8,
    key_frames: bool = True,
    jobs: int = 2,
    max_new: int | None = None,
    say: Say = print,
) -> dict[str, Observation]:
    """Observations for `rows` (from the cache where there is one), appending new ones
    to the cache as they come (at most `max_new` this run); later lines of the cache win.
    Events whose clip is gone are left out. One job per detector at most (a detector
    runs one frame at a time)."""
    known = load_cache(cache)
    rows = list(rows)
    todo = [row for row in rows if row.id not in known]
    if max_new is not None:
        todo = todo[: max(0, max_new)]
    upgrade = [row for row in rows if row.id in known and wants_dense(known[row.id])]
    out = dict(known)
    if not todo and not upgrade:
        return out
    if todo:
        say(f"looking at {len(todo)} clip(s) ({len(known)} cached in {cache})")
    cache.parent.mkdir(parents=True, exist_ok=True)
    pool: asyncio.Queue[ObjectDetector] = asyncio.Queue()
    for detector in detectors[: max(1, jobs)]:
        pool.put_nowait(detector)
    done = 0
    gone = 0

    async def one(row: EventRow, handle: Any) -> None:
        nonlocal done, gone
        case = await asyncio.to_thread(case_for_event, config, row)
        if case is None:
            gone += 1
            return
        detector = await pool.get()
        try:
            observation = await observe(case, detector, frames=frames, key_frames=key_frames)
        finally:
            pool.put_nowait(detector)
        out[row.id] = observation
        handle.write(json.dumps({"event": row.id, "obs": observation.model_dump()}) + "\n")
        handle.flush()
        done += 1
        if done % 100 == 0:
            say(f"  {done} of {len(todo)} looked at")

    async def densely(row: EventRow, handle: Any) -> None:
        """A cached observation from before the dense look, given it now."""
        clip = Path(row.clip_path) if row.clip_path else None
        if clip is None or not clip.is_file():
            return
        detector = await pool.get()
        try:
            observation = known[row.id].model_copy(deep=True)
            with tempfile.TemporaryDirectory(prefix="vidette-dense-") as tmp:
                await look_densely(clip, detector, observation, work_dir=Path(tmp))
        finally:
            pool.put_nowait(detector)
        out[row.id] = observation
        handle.write(json.dumps({"event": row.id, "obs": observation.model_dump()}) + "\n")
        handle.flush()

    with cache.open("a", encoding="utf-8") as handle:
        await asyncio.gather(
            *(one(row, handle) for row in todo), *(densely(row, handle) for row in upgrade)
        )
    if gone:
        say(f"  {gone} clip(s) no longer on disk (pruned): left out")
    return out


def pick(
    rows: list[EventRow], *, sample: float | None, seed: int = 7
) -> tuple[list[EventRow], dict[str, float]]:
    """Which events to look at: every event with anything known about it (an alert, a
    mark, a person or a face seen by the judge or deep analysis, a verdict that said
    alert) and a random `sample` share of the rest; the weight of each picked event for
    per-day estimates (1, or 1/sample)."""
    if sample is None or sample >= 1.0:
        return rows, {row.id: 1.0 for row in rows}
    chance = random.Random(seed)
    picked: list[EventRow] = []
    weights: dict[str, float] = {}
    for row in rows:
        if evidence_of(row):
            picked.append(row)
            weights[row.id] = 1.0
        elif chance.random() < sample:
            picked.append(row)
            weights[row.id] = 1.0 / sample
    return picked, weights


def stratified_weights(rows: Iterable[EventRow], observed: Iterable[str]) -> dict[str, float]:
    """How many events each observed one stands for: 1 for an event with evidence (all
    of those are looked at), and for the rest, per local day and camera, the events
    there over those looked at. Unbiased when the looked-at rest is a random subset,
    however far a run got."""
    seen = set(observed)
    total: Counter[tuple[str, str]] = Counter()
    looked: Counter[tuple[str, str]] = Counter()
    stratum: dict[str, tuple[str, str]] = {}
    weights: dict[str, float] = {}
    for row in rows:
        if evidence_of(row) is not None:
            if row.id in seen:
                weights[row.id] = 1.0
            continue
        key = (datetime.fromtimestamp(row.started_at).date().isoformat(), row.camera)
        total[key] += 1
        if row.id in seen:
            looked[key] += 1
            stratum[row.id] = key
    for event_id, key in stratum.items():
        weights[event_id] = total[key] / looked[key]
    return weights


@dataclass(frozen=True)
class Evidence:
    """What is known about one event, from the database."""

    alerted: bool = False  # confirmed: an alert (or one the digest kept)
    feedback: str | None = None  # 👍 up / 👎 down
    live_label: str | None = None
    live_level: str | None = None
    live_persons: int = 0
    live_said_alert: bool = False
    deep_label: str | None = None
    deep_level: str | None = None
    deep_persons: int = 0
    deep_vehicles: int = 0
    faces: int = 0  # readable faces (known or unknown people)
    unclear_faces: int = 0  # faces the face check itself could not read (often patterns)
    judged: bool = False
    early_only: bool = False  # the stored verdict is its early window's
    reasons: tuple[str, ...] = field(default=())

    @property
    def person(self) -> bool:
        return self.live_persons > 0 or self.deep_persons > 0 or self.faces > 0


def _int(value: Any) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def evidence_of(row: EventRow) -> Evidence | None:
    """What the database knows about an event that matters for a skip; None when
    nothing at all (never judged, no face, no mark)."""
    intent = row.intent or {}
    verdicts = [v for v in intent.get("verdicts") or [] if isinstance(v, Mapping)]
    live = next((v for v in verdicts if not v.get("abstained")), verdicts[0] if verdicts else {})
    deep = row.deep or {}
    deep_first: Any = (
        next(iter(deep.get("verdicts") or []), {}) if isinstance(deep, Mapping) else {}
    )
    people = intent.get("people") if isinstance(intent.get("people"), Mapping) else {}
    seen = [s for s in (people or {}).get("seen") or [] if isinstance(s, Mapping)]
    faces = sum(1 for s in seen if s.get("status") != "unclear")
    unclear = len(seen) - faces
    found = Evidence(
        alerted=row.state == "confirmed",
        feedback=row.feedback,
        live_label=live.get("label") if isinstance(live.get("label"), str) else None,
        live_level=live.get("level") if isinstance(live.get("level"), str) else None,
        live_persons=_int(live.get("persons")),
        live_said_alert=any(bool(v.get("model_said_alert")) for v in verdicts),
        deep_label=deep_first.get("label") if isinstance(deep_first, Mapping) else None,
        deep_level=deep_first.get("level") if isinstance(deep_first, Mapping) else None,
        deep_persons=_int(deep.get("persons")) if isinstance(deep, Mapping) else 0,
        deep_vehicles=_int(deep.get("vehicles")) if isinstance(deep, Mapping) else 0,
        faces=faces,
        unclear_faces=unclear,
        judged=bool(verdicts) and intent.get("phase") == "final",
        early_only=bool(verdicts) and intent.get("phase") == "early",
    )
    interesting = (
        found.alerted
        or found.feedback is not None
        or found.live_said_alert
        or found.person
        or found.unclear_faces > 0
        or found.deep_label is not None
        or found.live_level is not None
    )
    return found if interesting else None


# --- classifying and reporting --------------------------------------------------------------


@dataclass(frozen=True)
class Replayed:
    event_id: str
    camera: str
    at: float
    day: str
    klass: TriageClass
    why: str
    reason: str
    weight: float
    evidence: Evidence | None


def classify_all(
    rows: Iterable[EventRow],
    observations: Mapping[str, Observation],
    settings: TriageConfig,
    weights: Mapping[str, float],
    *,
    looked_share: float = 1.0,
) -> list[Replayed]:
    """Every observed event classified in time order, with the usual movement and the
    fixtures learned as the service would have learned them (from the clips looked at
    before). `looked_share`: the share of all clips looked at, so the fixtures' memory
    spans as much time as it would live."""
    memory = UsualMovement()
    fixtures = Fixtures(keep=max(1, round(FIXTURE_KEEP * looked_share)))
    out: list[Replayed] = []
    for row in sorted(rows, key=lambda r: (r.started_at, r.id)):
        observation = observations.get(row.id)
        if observation is None:
            continue
        usual = memory.usual(row.camera) if settings.usual_movement else None
        decision = classify(observation, settings, usual=usual, fixtures=fixtures.check(row.camera))
        if observation.checked and teaches(decision):
            memory.learn(row.camera, observation.cell_set())
        if observation.checked and decision.facts.person < settings.person:
            fixtures.learn(row.camera, weak_boxes(observation, settings))
        out.append(
            Replayed(
                event_id=row.id, camera=row.camera, at=row.started_at,
                day=datetime.fromtimestamp(row.started_at).date().isoformat(),
                klass=decision.klass, why=decision.why, reason=decision.reason,
                weight=weights.get(row.id, 1.0), evidence=evidence_of(row),
            )
        )  # fmt: skip
    return out


def reviewed_labels(config: VidetteConfig) -> dict[str, tuple[str, str]]:
    """Reviewed entries of the clip folders' `labels.yaml`: clip path → (scenario,
    dataset). Drafts are not truth and are left out."""
    found: dict[str, tuple[str, str]] = {}
    for folder in config.clip_folders:
        path = folder.path / "labels.yaml"
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        for entry in data.get("entries") or []:
            if not isinstance(entry, dict) or entry.get("status", "reviewed") != "reviewed":
                continue
            scenario = entry.get("scenario")
            if not isinstance(scenario, str) or scenario == "unlabelled":
                continue
            for media in entry.get("media") or []:
                clip = (folder.path / str(media)).resolve()
                if clip.suffix.lower() in (".mp4", ".mov", ".mkv"):
                    found[str(clip)] = (scenario, folder.path.name)
    return found


def alerting_scenarios(config: VidetteConfig) -> dict[str, str]:
    """Scenario → level, for every level that wakes someone (`wake` other than never)."""
    out: dict[str, str] = {}
    for policy in config.policies:
        for name, level in policy.levels.items():
            if level.wake is not Wake.never:
                out.update(dict.fromkeys(level.scenarios, name))
    return out


@dataclass
class Check:
    """One kind of evidence: how many events have it, how many the pre-filter would
    have skipped (and which), and how many it would have put last."""

    name: str
    must_be_zero: bool
    total: int = 0
    later: int = 0
    skipped: list[Replayed] = field(default_factory=list)


def checks(
    replayed: list[Replayed], rows: Mapping[str, EventRow], config: VidetteConfig
) -> list[Check]:
    alerting = alerting_scenarios(config)
    labels = reviewed_labels(config)
    table = [
        Check("alerts the bot sent, marked 👍", True),
        Check("alerts the bot sent, not marked", True),
        Check("alerts the bot sent, marked 👎", False),
        Check("reviewed labels of a waking scenario", True),
        Check("live judge: a waking scenario", True),
        Check("deep analysis: a waking scenario", True),
        Check("readable faces found by the face check", True),
        Check("unclear faces (the face check could not read them)", False),
        Check("people counted by the live judge", False),
        Check("people counted by deep analysis", False),
        Check("reviewed labels, any scenario", False),
    ]
    by_name = {c.name: c for c in table}
    for item in replayed:
        ev = item.evidence
        row = rows[item.event_id]
        label = labels.get(str(Path(row.clip_path).resolve())) if row.clip_path else None
        hits: list[str] = []
        if ev is not None:
            if ev.alerted and ev.feedback == "up":
                hits.append("alerts the bot sent, marked 👍")
            elif ev.alerted and ev.feedback is None:
                hits.append("alerts the bot sent, not marked")
            elif ev.alerted:
                hits.append("alerts the bot sent, marked 👎")
            if ev.live_label in alerting:
                hits.append("live judge: a waking scenario")
            if ev.deep_label in alerting:
                hits.append("deep analysis: a waking scenario")
            if ev.faces:
                hits.append("readable faces found by the face check")
            if ev.unclear_faces:
                hits.append("unclear faces (the face check could not read them)")
            if ev.live_persons:
                hits.append("people counted by the live judge")
            if ev.deep_persons:
                hits.append("people counted by deep analysis")
        if label is not None:
            hits.append("reviewed labels, any scenario")
            if label[0] in alerting:
                hits.append("reviewed labels of a waking scenario")
        for name in hits:
            check = by_name[name]
            check.total += 1
            if item.klass is TriageClass.skip:
                check.skipped.append(item)
            elif item.klass is TriageClass.later:
                check.later += 1
    return table


def per_day(replayed: list[Replayed]) -> dict[str, Counter[str]]:
    days: dict[str, Counter[str]] = {}
    for item in replayed:
        row = days.setdefault(item.day, Counter())
        row["looked"] += 1
        row["events"] += round(item.weight)
        row[item.klass.value] += round(item.weight)
    return dict(sorted(days.items()))


def per_reason(replayed: list[Replayed]) -> Counter[str]:
    out: Counter[str] = Counter()
    for item in replayed:
        out[f"{item.klass.value}: {item.why}"] += round(item.weight)
    return out


# --- the live queue, simulated --------------------------------------------------------------


@dataclass(frozen=True)
class Arrival:
    ready: float  # when the judge could take it (the clip closed; plus the look)
    ended: float  # what its age is counted from
    klass: TriageClass
    early: bool = False
    reason: str | None = None  # the pre-filter's (people before moving vehicles)

    @property
    def rank(self) -> int:
        return rank(self.klass, self.reason)


@dataclass
class Simulated:
    judged_final: int = 0
    judged_early: int = 0
    finals: int = 0
    by_class: Counter[str] = field(default_factory=Counter)  # judged finals by class
    offered: Counter[str] = field(default_factory=Counter)  # finals by class
    by_rank: Counter[int] = field(default_factory=Counter)  # judged finals by rank
    offered_rank: Counter[int] = field(default_factory=Counter)
    busy_s: float = 0.0

    def share(self, klass: str | None = None) -> float:
        if klass is None:
            return self.judged_final / self.finals if self.finals else 0.0
        offered = self.offered.get(klass, 0)
        return self.by_class.get(klass, 0) / offered if offered else 0.0


def simulate(
    arrivals: list[Arrival],
    durations: list[float],
    *,
    max_age_s: float,
    later_max_age_s: float,
    turns: Sequence[tuple[float, float]] = (),
) -> Simulated:
    """One slot, one verdict at a time, as the worker takes them: the best class first,
    newest first in it, `later` only when nothing else waits, too old ones dropped.
    `durations` are measured seconds per verdict, used in turn; `turns` are blocks of
    time the slot is taken by something else (deep analysis' daytime turns)."""
    result = Simulated()
    for arrival in arrivals:
        if not arrival.early and arrival.klass is not TriageClass.skip:
            result.offered[arrival.klass.value] += 1
        if not arrival.early:
            result.finals += 1
            result.offered_rank[arrival.rank] += 1
            if arrival.klass is TriageClass.skip:
                result.offered["skip"] += 1
    queue = sorted((a for a in arrivals if a.klass is not TriageClass.skip), key=lambda a: a.ready)
    blocks = sorted(turns)
    waiting: list[Arrival] = []
    t = queue[0].ready if queue else 0.0
    index = 0
    served = 0
    while index < len(queue) or waiting:
        while index < len(queue) and queue[index].ready <= t:
            waiting.append(queue[index])
            index += 1
        while blocks and blocks[0][0] <= t:
            start, length = blocks.pop(0)
            t = max(t, start) + length
            result.busy_s += length
        waiting = [
            a for a in waiting
            if t - a.ended <= (later_max_age_s if a.klass is TriageClass.later else max_age_s)
        ]  # fmt: skip
        if not waiting:
            if index < len(queue):
                t = max(t, queue[index].ready)
                continue
            break
        best = min(a.rank for a in waiting)
        pick = max((a for a in waiting if a.rank == best), key=lambda a: a.ended)
        waiting.remove(pick)
        took = durations[served % len(durations)] if durations else 45.0
        served += 1
        t += took
        result.busy_s += took
        if pick.early:
            result.judged_early += 1
        else:
            result.judged_final += 1
            result.by_class[pick.klass.value] += 1
            result.by_rank[pick.rank] += 1
    return result


@dataclass(frozen=True)
class DaySimulation:
    day: str
    events: int
    actual_judged: int  # whole clips the live judge really judged that day
    before: Simulated
    after: Simulated


EARLY_S = 8.0  # `vidette collect --early 8s`: the window a deployment like this one uses
EARLY_PREROLL_S = 3.0  # continuous mode's pre-roll comes before the motion's 8 s


def _ready(row: EventRow) -> tuple[float, float | None]:
    """When the clip was there for the judge, and when its early window was (None: no
    early window), from the clip's sidecar; without one, the event's end. An early
    window exists once a recording ran `EARLY_S` (a motion event in continuous mode: 8 s
    of motion after its pre-roll); the time the service recorded wins when it has one."""
    ended = row.ended_at or row.started_at
    early = ((row.intent or {}).get("timeline") or {}).get("early")
    if row.clip_path:
        meta = load_meta(Path(row.clip_path).with_suffix(".json"))
        if meta is not None:
            closed = float(meta.timeline.get("closed", ended))
            start = meta.started_at.timestamp()
            lead = EARLY_S + (EARLY_PREROLL_S if meta.trigger == "motion" else 0.0)
            if early is None and meta.duration_s >= lead + 1.0:
                early = start + lead
            return closed, None if early is None else float(early)
    return ended, None if early is None else float(early)


def measured_durations(rows: Iterable[EventRow]) -> list[float]:
    """Seconds per whole-clip verdict, as the live judge measured them, in order."""
    out: list[float] = []
    for row in sorted(rows, key=lambda r: r.started_at):
        intent = row.intent or {}
        timeline = intent.get("timeline") or {}
        start, end = timeline.get("judge_started"), timeline.get("judge_finished")
        if not isinstance(start, float | int) or not isinstance(end, float | int):
            continue
        took = float(end) - float(start)
        if intent.get("phase") == "final" and 0 < took < 900:
            out.append(took)
    return out


def simulate_days(
    rows: list[EventRow],
    replayed: list[Replayed],
    config: VidetteConfig,
    *,
    durations: list[float],
    turn_s: float,
    seed: int = 11,
) -> list[DaySimulation]:
    """Each day's clips through the live queue twice: as it ran (every clip `now`,
    early windows judged too) and with the pre-filter (its classes; an early window only
    when its clip has a person, an over-estimate of their number). Events that were not
    looked at get a class drawn from those looked at on the same day and camera.
    Deep analysis' daytime turns take `turn_s` every `trickle_every` from the end of
    its window to its start, in both runs."""
    classes = {item.event_id: item for item in replayed}
    pools: dict[tuple[str, str], list[Replayed]] = {}
    for item in replayed:
        if item.evidence is None:
            pools.setdefault((item.day, item.camera), []).append(item)
    chance = random.Random(seed)
    triage = config.understanding.triage
    deep = config.understanding.deep
    by_day: dict[str, list[EventRow]] = {}
    for row in rows:
        by_day.setdefault(datetime.fromtimestamp(row.started_at).date().isoformat(), []).append(row)
    out: list[DaySimulation] = []
    for day, day_rows in sorted(by_day.items()):
        before: list[Arrival] = []
        after: list[Arrival] = []
        actual = 0
        for row in day_rows:
            intent = row.intent or {}
            if intent.get("phase") == "final" and intent.get("verdicts"):
                actual += 1
            ready, early = _ready(row)
            ended = row.ended_at or row.started_at
            known = classes.get(row.id)
            if known is None:
                pool = pools.get((day, row.camera))
                drawn = chance.choice(pool) if pool and evidence_of(row) is None else None
                klass = drawn.klass if drawn is not None else TriageClass.now
                reason = drawn.reason if drawn is not None else "person"
            else:
                klass, reason = known.klass, known.reason
            before.append(Arrival(ready, ended, TriageClass.now))
            after.append(Arrival(ready + 2.0, ended, klass, reason=reason))
            if early is not None:
                before.append(Arrival(early, early, TriageClass.now, early=True))
                if reason in ("person", "possible_person"):
                    after.append(
                        Arrival(early + 2.0, early, TriageClass.now, early=True, reason=reason)
                    )
        start = datetime.fromisoformat(day).timestamp()
        turns: list[tuple[float, float]] = []
        if deep.enabled and deep.outside_window == "trickle":
            t = start
            while t < start + 86400:
                minute = int((t - start) // 60)
                if not deep.in_window(minute):
                    turns.append((t, turn_s))
                t += deep.trickle_every_s
        out.append(
            DaySimulation(
                day=day,
                events=len(day_rows),
                actual_judged=actual,
                before=simulate(
                    before, durations, max_age_s=900.0, later_max_age_s=900.0, turns=turns
                ),
                after=simulate(
                    after,
                    durations,
                    max_age_s=900.0,
                    later_max_age_s=triage.later_max_age_s,
                    turns=turns,
                ),
            )
        )
    return out
