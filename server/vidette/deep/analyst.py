"""Look, then decide: the deep analysis of one case (ADR-0019).

Two kinds of request, on the judge's OpenAI-compatible endpoint:

1. **Look** (frames, no policy): up to `frames_per_call` frames at a time, spread over the
   whole clip where the picture changed (vidette.verdicts.frames), with their times, the
   stalls between them and the face check's line. The model writes what happens, in
   order: who is where, what their hands do, where they go, which vehicles move or stop.
   The policy is left out on purpose: the model describes before it judges, and the
   frames get the room a shared server's small slot has (the live judge spends half of
   it on the policy).
2. **Decide** (text, no frames): the policy and its levels, the facts of the clip and
   everything the looks found. The answer is the live judge's schema with a longer
   description in the owner's language, so the verdict maps the same way
   (`VlmJudge._level_verdict`): a scenario that no frame shows is an abstention.

A look refused for context size is split in half and asked again. Nothing here waits
for a slot or decides when to run: every request is made inside `slot()` (the worker's
shared slot and server check, vidette.vlm.gate). Looks already made for a job that a
live call interrupted can be passed back (`resume`) and are not asked again.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from vidette.core.config import OTHER_SCENARIO, VlmBackend
from vidette.people.report import people_line
from vidette.verdicts.base import Case, FrameSet, PolicyText, Verdict
from vidette.verdicts.frames import frame_set, to_jpegs
from vidette.verdicts.vlm import (
    VERDICT_SCHEMA,
    VlmJudge,
    describe_case,
    levels_block,
    levels_schema,
    timing_text,
)
from vidette.vlm.client import VlmClient, VlmError

LOOK_SYSTEM = (
    "You look closely at frames from one event of a home security camera, for the person "
    "who lives there. Describe what happens, in time order: who or what is where (on the "
    "street or the pavement, at the gate, in the yard, at a door, a window or a car), what "
    "each person does with their hands, where they go, and which vehicles move, stop or "
    "park. Night frames are often grey infrared images. Describe actions, never looks: say "
    "a person, not a guessed gender, age or ethnicity. Say only what the frames show; if "
    "nothing happens, say so."
)

DECIDE_SYSTEM = (
    "You review one event from a home security camera on behalf of the person who lives "
    "there. You do not see the frames; you get what a careful look at them found, in time "
    "order, the facts of the clip and their alert policy in their own words. Decide whether, "
    "under that policy, the event deserves an alert. Judge only what the look reports; do "
    "not assume intent it does not describe. If it does not show enough to decide, answer "
    '"unsure". A person whose position changes between frames is walking, not lingering. '
    "Ordinary use is not an attempt: opening a car or a door and getting in or out, loading "
    "a car, unlocking a door and walking in is someone using it. An attempt needs something "
    "visibly wrong: a handle pulled that stays shut, several doors or cars tried one after "
    "another, force, tools, a broken window. Name a scenario that alerts only when the look "
    "reports it; otherwise name the closest scenario that does not alert. Describe what "
    "people do, not how they look."
)

LOOK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "observations": {"type": "string"},
        "persons": {"type": "integer"},
        "vehicles": {"type": "integer"},
        "subject_frames": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["observations", "persons", "vehicles", "subject_frames"],
    "additionalProperties": False,
}

MAX_OBSERVATIONS = 900  # characters of one look kept for the decision and the record

Slot = Callable[[], AbstractAsyncContextManager[None]]  # held around every request


@dataclass
class Look:
    """What one request found in frames `first`..`last` (1-based, of the whole set)."""

    first: int
    last: int
    observations: str
    persons: int | None = None
    vehicles: int | None = None
    subject: list[int] = field(default_factory=list)


@dataclass
class Call:
    kind: str  # "look" or "decide"
    seconds: float
    tokens_in: int | None = None
    tokens_out: int | None = None
    images: int = 0
    error: str | None = None


@dataclass
class DeepResult:
    frames: FrameSet
    looks: list[Look]
    verdicts: list[tuple[PolicyText, Verdict]]
    calls: list[Call]
    model: str | None = None

    @property
    def persons(self) -> int | None:
        counts = [look.persons for look in self.looks if look.persons is not None]
        return max(counts) if counts else None

    @property
    def vehicles(self) -> int | None:
        counts = [look.vehicles for look in self.looks if look.vehicles is not None]
        return max(counts) if counts else None


class DeepError(Exception):
    """The analysis could not finish; `retry` when a later attempt may succeed."""

    def __init__(self, message: str, *, retry: bool) -> None:
        super().__init__(message)
        self.retry = retry


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return max(0, int(value))


def _sub(frames: FrameSet, start: int, end: int) -> FrameSet:
    times = frames.times if len(frames.times) == len(frames.paths) else []
    return replace(
        frames,
        paths=frames.paths[start:end],
        times=times[start:end] if times else [],
    )


def _span(frames: FrameSet, first: int, last: int) -> str:
    times = [t for _p, t in frames.timed()[first - 1 : last] if t is not None]
    if not times:
        return f"frames {first}-{last}"
    return f"frames {first}-{last}, {times[0]:.1f} to {times[-1]:.1f} s"


def clip_facts(case: Case, frames: FrameSet) -> str:
    """Camera, local time, length and frame times, for a request without the frames."""
    incident = case.incident
    lines = [f"Camera: {incident.camera}."]
    if incident.started_at > 0:
        lines.append(f"Local time: {datetime.fromtimestamp(incident.started_at):%A %H:%M}.")
    duration = incident.duration_s or frames.duration_s
    if duration:
        lines.append(f"Recording: about {duration:.0f} s (not how long anyone stayed).")
    text = " ".join(lines)
    told = timing_text(frames)
    return f"{text}\n{told}" if told else text


class DeepAnalyst:
    def __init__(
        self,
        client: VlmClient,
        *,
        frames: int = 8,
        frames_per_call: int = 8,
        language: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.frames = frames
        self.frames_per_call = max(1, frames_per_call)
        self.language = language
        self._clock = clock
        self.last_looks: list[Look] = []  # of the current or last case, kept on failure

    async def analyse(
        self,
        case: Case,
        policies: list[PolicyText],
        *,
        work_dir: Path,
        slot: Slot | None = None,
        resume: list[Look] | None = None,
    ) -> DeepResult:
        try:
            frames = await frame_set(case, self.frames, work_dir=work_dir, out_dir=work_dir)
        except OSError as exc:
            raise DeepError(f"no frames: {exc}", retry=False) from exc
        except Exception as exc:  # MediaToolError and friends: the clip cannot be read
            raise DeepError(f"no frames: {type(exc).__name__}: {exc}", retry=False) from exc
        if not frames.paths:
            raise DeepError("no frames to look at", retry=False)
        result = DeepResult(frames=frames, looks=[], verdicts=[], calls=[])
        self.last_looks = result.looks
        start = 0
        size = self.frames_per_call
        for done in sorted(resume or [], key=lambda k: k.first):
            if done.first == start + 1 and done.last <= len(frames.paths):
                result.looks.append(done)
                start = done.last
        while start < len(frames.paths):
            end = min(len(frames.paths), start + size)
            look = await self._look(case, frames, start, end, result, slot)
            if look is None:  # refused for size: the same frames, fewer at a time
                if end - start <= 1:
                    raise DeepError("one frame does not fit the model's context", retry=False)
                size = max(1, (end - start) // 2)
                continue
            result.looks.append(look)
            start = end
        for policy in policies:
            verdict = await self._decide(case, policy, result, slot)
            result.verdicts.append((policy, verdict))
        return result

    async def _request(
        self,
        kind: str,
        result: DeepResult,
        slot: Slot | None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        images = kwargs.get("images") or []
        async with slot() if slot is not None else contextlib.nullcontext():
            started = self._clock()
            try:
                answer = await self.client.ask(**kwargs)
            except VlmError as exc:
                result.calls.append(
                    Call(kind, round(self._clock() - started, 2), images=len(images),
                         error=f"{exc.kind}: {exc}"[:200])
                )  # fmt: skip
                raise
        result.calls.append(
            Call(kind, answer.latency_s, answer.tokens_in, answer.tokens_out, answer.images)
        )
        result.model = answer.model
        return answer.data

    async def _look(
        self,
        case: Case,
        frames: FrameSet,
        start: int,
        end: int,
        result: DeepResult,
        slot: Slot | None,
    ) -> Look | None:
        chunk = _sub(frames, start, end)
        endpoint = self.client.endpoint
        try:
            images = await to_jpegs(chunk.paths, max_side=endpoint.max_image_side)
        except OSError as exc:
            raise DeepError(f"frames unreadable: {exc}", retry=False) from exc
        except Exception as exc:
            raise DeepError(f"frames unreadable: {type(exc).__name__}: {exc}", retry=False) from exc
        facts = describe_case(case, len(images), chunk)
        hint = people_line(case.people, names=endpoint.backend is VlmBackend.local)
        first, last = start + 1, end
        text = (
            f"{facts}\n{hint}\n\n".replace("\n\n\n", "\n\n")
            + f"These are frames {first} to {last} of the {len(frames.paths)} chosen from the "
            "clip (numbered from 1 over the whole clip).\n\n"
            "Answer with JSON: observations (what happens in these frames, in order, 2 to 6 "
            "short sentences in English), persons (how many different people are visible, 0 "
            "if none), vehicles (how many vehicles move, stop or park, 0 if none), "
            f"subject_frames (the numbers, from {first} to {last}, of the frames that clearly "
            "show a person or a vehicle doing something; [] when none does)."
        )
        try:
            data = await self._request(
                "look", result, slot,
                system=LOOK_SYSTEM, text=text, images=images, schema=LOOK_SCHEMA,
                schema_name="deep_look",
            )  # fmt: skip
        except VlmError as exc:
            message = str(exc).lower()
            if "context size" in message or "exceed_context" in message:
                return None
            raise DeepError(f"{exc.kind}: {exc}", retry=exc.kind != "bad_answer") from exc
        subject = [
            int(n)
            for n in data.get("subject_frames") or []
            if isinstance(n, int | float) and not isinstance(n, bool) and first <= n <= last
        ]
        return Look(
            first=first,
            last=last,
            observations=" ".join(str(data.get("observations") or "").split())[:MAX_OBSERVATIONS],
            persons=_count(data.get("persons")),
            vehicles=_count(data.get("vehicles")),
            subject=subject,
        )

    async def _decide(
        self,
        case: Case,
        policy: PolicyText,
        result: DeepResult,
        slot: Slot | None,
    ) -> Verdict:
        endpoint = self.client.endpoint
        frames = result.frames
        quiet = policy.known_people == "quiet" and not policy.levels
        hint = people_line(case.people, names=endpoint.backend is VlmBackend.local, quiet=quiet)
        found = "\n".join(
            f"- {_span(frames, look.first, look.last)}: {look.observations or 'nothing reported'}"
            for look in result.looks
        )
        facts = clip_facts(case, frames)
        if hint:
            facts += f"\n{hint}"
        language = f" in {self.language}" if self.language else ""
        description = (
            f"2 to 4 short sentences{language} for the person who lives here: what happened, "
            "where, and whether it matters; plain words, no ids or codes"
        )
        if policy.levels:
            text = (
                f'Policy "{policy.name}": {" ".join(policy.description.split())}\n\n'
                f"{levels_block(policy)}\n\n{facts}\n\n"
                f"What a careful look at {len(frames.paths)} frames of the clip found:\n"
                f"{found}\n\n"
                "Answer with JSON: scenario (one id from the list, or unsure when this does "
                f"not show enough), description ({description}), persons (how many different "
                "people, 0 if none), first_person_frame (the number of the first frame with a "
                "person, 0 if none), subject_frames (the numbers of the frames that show what "
                "your scenario is about, the clearest first; [] when none does), confidence "
                "(0 to 1)."
            )
            schema, name = levels_schema(policy), "deep_level_verdict"
        else:
            text = (
                f'Policy "{policy.name}": {" ".join(policy.description.split())}\n\n'
                f"{facts}\n\nWhat a careful look at {len(frames.paths)} frames of the clip "
                f"found:\n{found}\n\n"
                "Answer with JSON: decision (alert, silent or unsure), label (a short "
                f"snake_case name for what happened), description ({description}), "
                "confidence (0 to 1)."
            )
            schema, name = VERDICT_SCHEMA, "deep_verdict"
        try:
            data = await self._request(
                "decide", result, slot,
                system=DECIDE_SYSTEM, text=text, images=[], schema=schema, schema_name=name,
            )  # fmt: skip
        except VlmError as exc:
            raise DeepError(f"{exc.kind}: {exc}", retry=exc.kind != "bad_answer") from exc
        decide = result.calls[-1]
        measured: dict[str, Any] = {
            "vlm_calls": len(result.calls),
            "images_sent": sum(c.images for c in result.calls),
            "latency_s": round(sum(c.seconds for c in result.calls), 2),
            "tokens_in": decide.tokens_in,
            "tokens_out": decide.tokens_out,
            "model": result.model,
        }
        if policy.levels:
            return VlmJudge._level_verdict(data, policy, measured, frames)
        decision = str(data.get("decision", "")).lower()
        label = data.get("label")
        said = data.get("description")
        unsure = decision not in ("alert", "silent")
        confidence = data.get("confidence")
        return Verdict(
            alert=decision == "alert",
            abstained=unsure,
            label=str(label)[:80] if label else (None if unsure else OTHER_SCENARIO),
            confidence=(
                min(1.0, max(0.0, float(confidence)))
                if isinstance(confidence, int | float) and not isinstance(confidence, bool)
                else None
            ),
            rationale=str(said)[:600] if said else None,
            **measured,
        )
