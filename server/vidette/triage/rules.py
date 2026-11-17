"""Now, later or skip: the pre-filter's rules, pure functions over an `Observation`.

First match wins:

1. The detector did not look (none configured, no frames, an error): **now**, or
   **later** for tiny motion and light. Nothing is skipped without a detector check.
2. A person: **now**. The policies are about people.
3. A weaker person candidate (`possible_person` to `person`): **now**. Unclear goes to
   the judge, not to the bin.
4. A vehicle that moves (not in the same place in every frame): **now** (or **later**
   with `vehicles: later`). A car hit while parking, a car driving in.
5. An animal: **later**. Never an alert by itself; the judge may still see more.
5a. The light changed (a lamp switched on) while things moved, nobody found: **now**. A
    floodlight's own sensor saw something warm; the detector can miss a person
    over-exposed at the frame's edge.
6. An early window with none of the above: **skip**. The whole clip follows and is
   looked at again.
7. Fewer than `MIN_FRAMES` frames looked at: **later**. Too little to throw it away.
8. Light change, the picture brightened or darkened and little else changed: **skip**.
9. Tiny motion, almost nothing changed (and the motion gate agrees): **skip**.
10. The usual movement, everything that changed is where things always move on this
    camera (a tree, an awning): **skip**.
11. Anything else, something moved and nothing was recognised: **later**.

Within *now*, people go before weaker person candidates and moving vehicles with nobody
seen (`rank`): the judge's time goes to people first.

Parked cars are in every frame at the same place and do not count as vehicles. Nothing
here knows about scenarios or levels: a clip that could matter to any of them is never
skipped, because a skip needs the detector to have looked and found nobody (a policy
level with `wake: always` included).

The usual movement is learned per camera from clips where the detector found nothing:
a cell of the 64×36 activity grid that changed in at least `USUAL_SHARE` of the last
`USUAL_KEEP` such clips is "usual". A person is never part of it (a clip with a person is
not learned from), and a clip in which anything changed outside the usual cells is not
skipped by this rule.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from vidette.core.config import TriageConfig
from vidette.triage.detector import Detection, iou
from vidette.triage.observe import Observation

MIN_FRAMES = 3  # a skip needs at least this many frames looked at
STILL_IOU = 0.6  # a vehicle box this alike in another frame has not moved
TINY = 0.01  # activity map: below this share changed (about 23 of 2304 cells) is tiny
TINY_GATE = 0.004  # the continuous mode's gate: largest changed share for tiny motion
LIGHT = 0.06  # a global brightness change of this much (0..1) with...
LIGHT_RESIDUAL = 0.03  # ...at most this share changed beyond it is a light change
USUAL_KEEP = 300  # nothing-detected clips per camera the usual movement is learned from
USUAL_MIN = 30  # fewer than this learned: no usual movement yet
USUAL_SHARE = 0.2  # a cell that changed in this share of them is usual
USUAL_OUTSIDE = 3  # at most this many changed cells outside the usual ones


class TriageClass(StrEnum):
    now = "now"
    later = "later"
    skip = "skip"


ORDER = {TriageClass.now: 0, TriageClass.later: 1, TriageClass.skip: 2}
RANK_PEOPLE, RANK_SECOND, RANK_LATER, RANK_SKIP = range(4)
SECOND_TIER = frozenset({"vehicle", "possible_person"})


def rank(klass: TriageClass, reason: str | None) -> int:
    """The order the live judge takes clips in, newest first within each: *now* with a
    person (or not checked) first; then *now* for a weaker person candidate or a moving
    vehicle with nobody seen (on a test deployment nine in ten of each were ordinary
    traffic or no person at all); then *later*. Skipped clips are never taken."""
    if klass is TriageClass.skip:
        return RANK_SKIP
    if klass is TriageClass.later:
        return RANK_LATER
    return RANK_SECOND if reason in SECOND_TIER else RANK_PEOPLE


@dataclass(frozen=True)
class Facts:
    """What the observation amounts to (stored with the decision)."""

    person: float = 0.0  # best person score in any frame
    vehicle_moving: float = 0.0  # best score of a vehicle that moved
    vehicles_still: int = 0  # vehicles in the same place in every frame they are in
    animal: float = 0.0
    frames: int = 0
    usual: float | None = None  # share of the changed cells that are usual here

    def record(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "person": round(self.person, 3),
            "vehicle_moving": round(self.vehicle_moving, 3),
            "vehicles_still": self.vehicles_still,
            "animal": round(self.animal, 3),
            "frames": self.frames,
        }
        if self.usual is not None:
            out["usual"] = round(self.usual, 3)
        return out


@dataclass(frozen=True)
class Decision:
    klass: TriageClass
    why: str  # a few plain words, shown and stored
    reason: str  # a stable code: person, possible_person, vehicle, animal, early, light, …
    facts: Facts = field(default_factory=Facts)

    def record(self, observation: Observation, *, phase: str) -> dict[str, Any]:
        """What is kept on the event (`intent.triage`)."""
        return {
            "v": 1,
            "class": self.klass.value,
            "why": self.why,
            "reason": self.reason,
            "phase": phase,
            "facts": self.facts.record(),
            "observed": {
                "detector": observation.detector,
                "trigger": observation.trigger,
                "duration_s": observation.duration_s,
                "motion_peak": observation.motion_peak,
                "activity_peak": observation.activity_peak,
                "light": observation.light,
                "cells": observation.cells,
                "error": observation.error,
                "seconds": observation.seconds,
            },
        }


def _moved(detection: Detection, frames: list[list[Detection]], own: int) -> bool:
    """A vehicle moved when some other frame has nothing of its kind where it is."""
    others = [dets for index, dets in enumerate(frames) if index != own]
    if not others:
        return True
    return any(
        not any(d.kind == detection.kind and iou(d.box, detection.box) >= STILL_IOU for d in dets)
        for dets in others
    )


def facts_of(
    observation: Observation, settings: TriageConfig, usual: frozenset[int] | None = None
) -> Facts:
    frames = [frame.detections() for frame in observation.frames]
    person = max((d.score for dets in frames for d in dets if d.kind == "person"), default=0.0)
    animal = max((d.score for dets in frames for d in dets if d.kind == "animal"), default=0.0)
    moving = 0.0
    still = 0
    for index, dets in enumerate(frames):
        for detection in dets:
            if detection.kind != "vehicle" or detection.score < settings.vehicle:
                continue
            if _moved(detection, frames, index):
                moving = max(moving, detection.score)
            elif index == 0 or not any(
                iou(detection.box, d.box) >= STILL_IOU
                for earlier in frames[:index]
                for d in earlier
                if d.kind == "vehicle" and d.score >= settings.vehicle
            ):
                still += 1
    share = None
    if usual is not None:
        cells = observation.cell_set()
        if cells:
            share = len(cells & usual) / len(cells)
    return Facts(person, moving, still, animal, len(frames), share)


def classify(
    observation: Observation,
    settings: TriageConfig,
    *,
    partial: bool = False,
    usual: frozenset[int] | None = None,
) -> Decision:
    """The class of one clip (see the module docstring for the table)."""
    facts = facts_of(observation, settings, usual)
    quiet = _quiet(observation)
    if not observation.checked:
        missing = observation.error or "no detector"
        if quiet is not None:
            return Decision(TriageClass.later, f"{quiet[1]}, not checked ({missing})",
                            "unchecked", facts)  # fmt: skip
        return Decision(TriageClass.now, f"not checked: {missing}", "unchecked", facts)
    if facts.person >= settings.person:
        return Decision(TriageClass.now, "a person", "person", facts)
    if facts.person >= settings.possible_person:
        return Decision(TriageClass.now, "maybe a person", "possible_person", facts)
    if facts.vehicle_moving > 0:
        klass = TriageClass(settings.vehicles)
        return Decision(klass, "a vehicle moves", "vehicle", facts)
    if facts.animal >= settings.animal:
        return Decision(TriageClass.later, "an animal", "animal", facts)
    light = observation.light or 0.0
    if light >= LIGHT and (observation.activity_peak or 0.0) > LIGHT_RESIDUAL:
        # a lamp switched on (a floodlight's own motion sensor) while things moved: on a
        # test deployment, a person at the gate at night, over-exposed at the frame's edge
        return Decision(
            TriageClass.now, "the light changed and something moved", "light_motion", facts
        )
    if not settings.skip:
        return Decision(TriageClass.later, "nobody seen", "nobody", facts)
    if partial:
        return Decision(
            TriageClass.skip, "early window, nobody yet; the whole clip decides", "early", facts
        )
    if facts.frames < MIN_FRAMES:
        return Decision(TriageClass.later, "too few frames to skip", "few_frames", facts)
    if quiet is not None:
        return Decision(TriageClass.skip, quiet[1], quiet[0], facts)
    if usual is not None and _only_usual(observation.cell_set(), usual):
        return Decision(TriageClass.skip, "only the usual movement here", "usual", facts)
    return Decision(TriageClass.later, "something moved, nobody seen", "motion", facts)


def _quiet(observation: Observation) -> tuple[str, str] | None:
    """Light change or tiny motion, from the activity map (and the gate, when there is
    one); None when there is more, or no activity map."""
    peak = observation.activity_peak
    if peak is None:
        return None
    if (observation.light or 0.0) >= LIGHT and peak <= LIGHT_RESIDUAL:
        return "light", "light change"
    gate = observation.motion_peak  # 256×144 at 4 fps: it sees smaller things than the map
    if peak < TINY and (gate is None or gate < TINY_GATE):
        return "tiny", "tiny motion"
    return None


def _only_usual(cells: frozenset[int], usual: frozenset[int]) -> bool:
    return bool(cells) and len(cells - usual) <= USUAL_OUTSIDE


class UsualMovement:
    """Where things always move, per camera, from the last clips with nobody in them."""

    def __init__(
        self, *, keep: int = USUAL_KEEP, minimum: int = USUAL_MIN, share: float = USUAL_SHARE
    ) -> None:
        self._keep = keep
        self._minimum = minimum
        self._share = share
        self._clips: dict[str, deque[frozenset[int]]] = {}
        self._counts: dict[str, dict[int, int]] = {}

    def learn(self, camera: str, cells: frozenset[int]) -> None:
        clips = self._clips.setdefault(camera, deque())
        counts = self._counts.setdefault(camera, {})
        clips.append(cells)
        for cell in cells:
            counts[cell] = counts.get(cell, 0) + 1
        while len(clips) > self._keep:
            for cell in clips.popleft():
                counts[cell] -= 1
                if not counts[cell]:
                    del counts[cell]

    def usual(self, camera: str) -> frozenset[int] | None:
        clips = self._clips.get(camera)
        if not clips or len(clips) < self._minimum:
            return None
        bar = self._share * len(clips)
        return frozenset(c for c, n in self._counts.get(camera, {}).items() if n >= bar)

    def learned(self, camera: str) -> int:
        return len(self._clips.get(camera) or ())


LEARN_FROM = frozenset({"early", "light", "tiny", "usual", "motion", "few_frames", "nobody"})


def teaches(decision: Decision) -> bool:
    """Whether a clip is one the usual movement learns from: the detector looked and
    found nobody, no moving vehicle and no animal."""
    return decision.reason in LEARN_FROM and decision.reason != "early"
