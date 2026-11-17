"""The VLM judge: frames + the person's policy in plain words → a structured verdict.

It sees what a person would see: a few frames of the incident, in time order, the
camera name and time of day, what the upstream NVR detected (if anything), and the
policy text. It never sees a dataset's ground truth (`alert_on`), and it is told the
same things live and in `vidette eval`, so offline numbers mean something at home.

The answer follows a JSON schema:

    {"decision": "alert" | "silent" | "unsure",
     "label": "courier_dropping_parcel",          short snake_case name of what happened
     "description": "A courier leaves a box …",   one sentence about what is visible
     "confidence": 0.82}

`unsure` is an abstention: the frames do not show enough. Abstentions, timeouts, bad
answers and an exhausted daily budget all count as "no alert" and never notify; the
verdict says which it was. Latency, tokens and estimated cost ride on every verdict.
"""

from __future__ import annotations

import itertools
import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from vidette.core.config import OTHER_SCENARIO, VlmBackend
from vidette.ffmpeg import MediaToolError
from vidette.people.report import people_line
from vidette.verdicts.base import Case, FrameSet, PolicyText, Verdict
from vidette.verdicts.frames import frame_set, to_jpegs
from vidette.vlm.client import VlmClient, VlmError

logger = logging.getLogger(__name__)

# Every token here is paid on every call, and a shared llama.cpp slot may hold only 2048
# with four frames taking about 900 of them: every rule is kept, each said once and short.
SYSTEM_PROMPT = (
    "You review frames from one event of a home security camera, in time order, for the "
    "person who lives there, under their alert policy. Decide if it deserves an alert. "
    "Judge only what the frames show; never assume intent. Night frames are often grey "
    "infrared. If they do not show enough, answer unsure; do not guess. "
    "A person whose position changes between frames is walking, not lingering; how long "
    "someone stayed shows only across frames with no stall between them (a stall freezes "
    "the picture). Ordinary use is not an attempt: getting into or out of a car or a door, "
    "loading a car, unlocking a door and walking in. An attempt needs something visibly "
    "wrong: a handle pulled that stays shut, several doors or cars tried in turn, force, "
    "tools, broken glass. Name an alerting scenario only when the frames show it, else the "
    "closest one that does not alert. Describe actions, not looks: a person, never a "
    "guessed gender, age or ethnicity."
)

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["alert", "silent", "unsure"]},
        "label": {"type": "string"},
        "description": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["decision", "label", "description", "confidence"],
    "additionalProperties": False,
}


def describe_case(case: Case, frames: int, timing: FrameSet | None = None) -> str:
    """The facts line block every VLM prompt starts with (shared with the labeller)."""
    incident = case.incident
    lines = [f"Camera: {incident.camera}."]
    if incident.started_at > 0:
        at = datetime.fromtimestamp(incident.started_at)
        lines.append(f"Local time: {at:%A %H:%M}.")
    duration = incident.duration_s or (timing.duration_s if timing is not None else None)
    if duration:
        lines.append(f"Recording: about {duration:.0f} s (not how long anyone stayed).")
    detected = ", ".join(incident.objects)
    if detected:
        where = f" in zones {', '.join(incident.zones)}" if incident.zones else ""
        lines.append(f"The camera's detector reported: {detected}{where}.")
    if case.context:
        lines.append(case.context)
    lines.append(f"{frames} frame(s) follow.")
    text = " ".join(lines)
    told = timing_text(timing) if timing is not None else None
    return f"{text}\n{told}" if told else text


def _s(seconds: float) -> str:
    return f"{seconds:.1f} s"


def timing_text(frames: FrameSet) -> str | None:
    """When each frame was taken and where the video stalled, for the judge (short: a
    shared server's slot holds about 2000 tokens with four frames)."""
    timed = [t for _path, t in frames.timed()]
    if not timed or any(t is None for t in timed):
        return None
    times = [float(t) for t in timed if t is not None]
    text = "Frame times: " + ", ".join(f"{n}: {_s(t)}" for n, t in enumerate(times, start=1))
    if frames.method == "activity":
        text += " (where the picture changed)"
    lines = [text + "."]
    if frames.note:
        lines.append(frames.note)
    health = frames.health
    if health is not None:
        for n, (before, after) in enumerate(itertools.pairwise(times), start=1):
            for start, end in health.gaps_between(before, after):
                lines.append(
                    f"The video stalled between frames {n} and {n + 1} "
                    f"({start:.1f} to {end:.1f} s): how much time passed is unknown."
                )
        if health.interrupted:
            lines.append(
                f"The recording was interrupted: {health.stalls} stall(s), "
                f"up to {_s(health.longest_stall_s)}."
            )
    return " ".join(lines)


def case_facts(
    case: Case,
    frames: int,
    *,
    backend: VlmBackend,
    quiet: bool = False,
    timing: FrameSet | None = None,
) -> str:
    """`describe_case` plus the local face check's line, if there is one. Names of the
    people recognized go only to a local model; a cloud model hears "a known person"."""
    facts = describe_case(case, frames, timing)
    hint = people_line(case.people, names=backend is VlmBackend.local, quiet=quiet)
    return f"{facts}\n{hint}" if hint else facts


def levels_schema(policy: PolicyText) -> dict[str, Any]:
    scenarios = [sid for level in policy.levels for sid, _ in level.scenarios]
    return {
        "type": "object",
        "properties": {
            "scenario": {"type": "string", "enum": [*scenarios, OTHER_SCENARIO, "unsure"]},
            "description": {"type": "string"},
            "persons": {"type": "integer"},
            "first_person_frame": {"type": "integer"},
            "subject_frames": {"type": "array", "items": {"type": "integer"}},
            "confidence": {"type": "number"},
        },
        "required": [
            "scenario", "description", "persons", "first_person_frame", "subject_frames",
            "confidence",
        ],
        "additionalProperties": False,
    }  # fmt: skip


def levels_block(policy: PolicyText) -> str:
    """The scenarios a policy with levels asks the judge to choose from."""
    lines = ["Scenarios by level, highest first; if several fit, take the highest."]
    for level in policy.levels:
        kind = "an alert" if level.alerts else "not an alert"
        lines.append(f"Level {level.name} ({kind}):")
        lines += [f"- {sid}: {' '.join(text.split())}" for sid, text in level.scenarios]
    lines.append(f"- {OTHER_SCENARIO}: none of the scenarios above (not an alert).")
    return "\n".join(lines)


def _persons(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return max(0, int(value))


CONTEXT_RETRY_SCALE = 0.8  # a prompt over the server's context: images this much smaller


def smaller_side(exc: VlmError, side: int, attempts: int) -> int | None:
    """After a refusal for context size (llama-server: "exceeds the available context
    size"), the image side for one more try; None for any other error or a second one."""
    text = str(exc).lower()
    if attempts != 1 or not ("context size" in text or "exceed_context" in text):
        return None
    return max(256, int(side * CONTEXT_RETRY_SCALE))


def _frame_time(value: object, frames: FrameSet) -> float | None:
    """The time in the clip of frame number `value` (1-based), when known."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 1:
        return None
    timed = frames.timed()
    index = int(value) - 1
    return timed[index][1] if index < len(timed) else None


def _subject_numbers(value: object, count: int) -> list[int] | None:
    """The frame numbers (1-based) the judge said show its subject, clearest first, only
    those that exist; None when it did not answer the question at all (an older prompt)."""
    if not isinstance(value, list):
        return None
    numbers: list[int] = []
    for number in value:
        if isinstance(number, bool) or not isinstance(number, int | float):
            continue
        if 1 <= int(number) <= count and int(number) not in numbers:
            numbers.append(int(number))
    return numbers


def _confidence(value: object) -> float | None:
    if isinstance(value, int | float):
        return min(1.0, max(0.0, float(value)))
    return None


@dataclass
class VlmJudge:
    """`name` is `vlm-local` or `vlm-cloud`: the id on the command line and in reports."""

    client: VlmClient
    frames_per_call: int = 4
    cache_dir: Path | None = None
    language: str | None = None  # of the one-line description; None: the model's English

    @property
    def name(self) -> str:
        return f"vlm-{self.client.endpoint.backend.value}"

    @property
    def description(self) -> str:
        endpoint = self.client.endpoint
        return f"VLM {endpoint.model} at {endpoint.host}, {self.frames_per_call} frames"

    async def judge(self, case: Case, policy: PolicyText) -> Verdict:
        with tempfile.TemporaryDirectory(prefix="vidette-frames-") as tmp:
            return await self._judge(case, policy, Path(tmp))

    async def _judge(self, case: Case, policy: PolicyText, tmp: Path) -> Verdict:
        endpoint = self.client.endpoint
        try:
            frames = await frame_set(case, self.frames_per_call, work_dir=self.cache_dir or tmp)
            images = await to_jpegs(frames.paths, max_side=endpoint.max_image_side)
        except (MediaToolError, OSError) as exc:
            return Verdict(alert=False, abstained=True, rationale=f"no frames: {exc}")
        if not images:
            return Verdict(alert=False, abstained=True, rationale="no frames to look at")
        facts = case_facts(
            case,
            len(images),
            backend=endpoint.backend,
            quiet=policy.known_people == "quiet" and not policy.levels,
            timing=frames,
        )
        sentence = "one sentence about what is visible"
        if self.language:
            sentence += f", in {self.language}"
        if policy.levels:
            text = (
                f'Policy "{policy.name}": {" ".join(policy.description.split())}\n\n'
                f"{levels_block(policy)}\n\n{facts}\n\n"
                "Answer with JSON: scenario (an id above, or unsure), description "
                f"({sentence}), persons (different people over all frames, 0 if none), "
                "first_person_frame (first frame with a person, 0 if none), subject_frames "
                "(frames that clearly show your scenario's person, animal or vehicle, clearest "
                "first; [] if none), confidence (0 to 1)."
            )
            schema, schema_name = levels_schema(policy), "level_verdict"
        else:
            text = (
                f'Policy "{policy.name}": {" ".join(policy.description.split())}\n\n'
                f"{facts}\n\n"
                "Answer with JSON: decision (alert, silent or unsure), label (a short "
                f"snake_case name for what happened), description ({sentence}), "
                "confidence (0 to 1)."
            )
            schema, schema_name = VERDICT_SCHEMA, "verdict"
        calls = 0
        while True:
            try:
                answer = await self.client.ask(
                    system=SYSTEM_PROMPT,
                    text=text,
                    images=images,
                    schema=schema,
                    schema_name=schema_name,
                )
                break
            except VlmError as exc:
                calls += 0 if exc.kind == "budget" else 1
                side = smaller_side(exc, endpoint.max_image_side, calls)
                if side is not None:
                    # the slot is too small for this prompt: the same frames, smaller
                    logger.info("%s: %s; retrying at %d px", self.name, exc, side)
                    try:
                        images = await to_jpegs(frames.paths, max_side=side)
                        continue
                    except (MediaToolError, OSError):
                        pass
                logger.info("%s abstained on %s: %s", self.name, case.incident.key, exc)
                return Verdict(
                    alert=False,
                    abstained=True,
                    rationale=f"{exc.kind}: {exc}",
                    vlm_calls=calls,
                    images_sent=len(images) * calls,
                )
        measured: dict[str, Any] = {
            "vlm_calls": answer.calls + calls,
            "images_sent": answer.images,
            "latency_s": answer.latency_s,
            "tokens_in": answer.tokens_in,
            "tokens_out": answer.tokens_out,
            "cost_usd": answer.usd,
            "model": answer.model,
        }
        if policy.levels:
            return self._level_verdict(answer.data, policy, measured, frames)
        decision = str(answer.data.get("decision", "")).lower()
        label = answer.data.get("label")
        description = answer.data.get("description")
        unsure = decision not in ("alert", "silent")
        return Verdict(
            alert=decision == "alert",
            abstained=unsure,
            label=str(label)[:80] if label else None,
            confidence=_confidence(answer.data.get("confidence")),
            rationale=(
                f"unsure: {description or 'the frames do not show enough'}"[:400]
                if unsure
                else (str(description)[:400] if description else None)
            ),
            **measured,
        )

    @staticmethod
    def _level_verdict(
        data: dict[str, Any], policy: PolicyText, measured: dict[str, Any], frames: FrameSet
    ) -> Verdict:
        scenario = str(data.get("scenario", "")).strip().lower()
        description = data.get("description")
        known = {sid for level in policy.levels for sid, _ in level.scenarios}
        if scenario not in known and scenario != OTHER_SCENARIO:
            said = "unsure" if scenario == "unsure" else f"unknown scenario '{scenario[:40]}'"
            return Verdict(
                alert=False,
                abstained=True,
                label=None,
                rationale=f"{said}: {description or 'the frames do not show enough'}"[:400],
                **measured,
            )
        level = policy.scenario_level(scenario)
        subject = _subject_numbers(data.get("subject_frames"), len(frames.paths))
        if level is not None and level.alerts and subject is not None and not subject:
            # an alert with no frame that shows it is an assumption: no alert (ADR-0017)
            return Verdict(
                alert=False,
                abstained=True,
                label=None,
                rationale=(
                    f"unsure: named {scenario} but pointed at no frame that shows it; "
                    f"{description or ''}"
                ).strip("; ")[:400],
                **measured,
            )
        return Verdict(
            alert=level is not None and level.alerts,
            label=scenario,
            level=None if level is None else level.name,
            persons=_persons(data.get("persons")),
            person_at_s=_frame_time(data.get("first_person_frame"), frames),
            subject_at_s=[
                at for at in (_frame_time(n, frames) for n in subject or []) if at is not None
            ],
            confidence=_confidence(data.get("confidence")),
            rationale=str(description)[:400] if description else None,
            **measured,
        )
