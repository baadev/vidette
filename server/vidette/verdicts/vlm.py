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

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from vidette.core.config import VlmBackend
from vidette.ffmpeg import MediaToolError
from vidette.people.report import people_line
from vidette.verdicts.base import Case, PolicyText, Verdict
from vidette.verdicts.frames import frame_jpegs
from vidette.vlm.client import VlmClient, VlmError

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You review footage from a home security camera on behalf of the person who lives "
    "there. You get a few frames from one event, in time order, and their alert policy in "
    "their own words. Decide whether, under that policy, this event deserves an alert on "
    "their phone. Judge only what the frames show; do not assume intent you cannot see. "
    "Night frames are often grey infrared images. If the frames do not show enough to "
    "decide, answer \"unsure\" rather than guessing."
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


def describe_case(case: Case, frames: int) -> str:
    """The facts line block every VLM prompt starts with (shared with the labeller)."""
    incident = case.incident
    lines = [f"Camera: {incident.camera}."]
    if incident.started_at > 0:
        at = datetime.fromtimestamp(incident.started_at)
        lines.append(f"Local time: {at:%A %H:%M}.")
    if incident.duration_s:
        lines.append(f"The event lasted about {incident.duration_s:.0f} s.")
    detected = ", ".join(incident.objects)
    if detected:
        where = f" in zones {', '.join(incident.zones)}" if incident.zones else ""
        lines.append(f"The camera's detector reported: {detected}{where}.")
    lines.append(f"{frames} frame(s) follow, in time order.")
    return " ".join(lines)


def case_facts(case: Case, frames: int, *, backend: VlmBackend, quiet: bool = False) -> str:
    """`describe_case` plus the local face check's line, if there is one. Names of the
    people recognized go only to a local model; a cloud model hears "a known person"."""
    facts = describe_case(case, frames)
    hint = people_line(case.people, names=backend is VlmBackend.local, quiet=quiet)
    return f"{facts}\n{hint}" if hint else facts


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

    @property
    def name(self) -> str:
        return f"vlm-{self.client.endpoint.backend.value}"

    @property
    def description(self) -> str:
        endpoint = self.client.endpoint
        return f"VLM {endpoint.model} at {endpoint.host}, {self.frames_per_call} frames"

    async def judge(self, case: Case, policy: PolicyText) -> Verdict:
        endpoint = self.client.endpoint
        try:
            images = await frame_jpegs(
                case,
                self.frames_per_call,
                max_side=endpoint.max_image_side,
                cache_dir=self.cache_dir,
            )
        except (MediaToolError, OSError) as exc:
            return Verdict(alert=False, abstained=True, rationale=f"no frames: {exc}")
        if not images:
            return Verdict(alert=False, abstained=True, rationale="no frames to look at")
        facts = case_facts(
            case, len(images), backend=endpoint.backend, quiet=policy.known_people == "quiet"
        )
        text = (
            f'Policy "{policy.name}": {" ".join(policy.description.split())}\n\n'
            f"{facts}\n\n"
            "Answer with JSON: decision (alert, silent or unsure), label (a short "
            "snake_case name for what happened), description (one sentence about what is "
            "visible), confidence (0 to 1)."
        )
        try:
            answer = await self.client.ask(
                system=SYSTEM_PROMPT,
                text=text,
                images=images,
                schema=VERDICT_SCHEMA,
                schema_name="verdict",
            )
        except VlmError as exc:
            logger.info("%s abstained on %s: %s", self.name, case.incident.key, exc)
            return Verdict(
                alert=False,
                abstained=True,
                rationale=f"{exc.kind}: {exc}",
                vlm_calls=0 if exc.kind == "budget" else 1,
                images_sent=0 if exc.kind == "budget" else len(images),
            )
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
            vlm_calls=answer.calls,
            images_sent=answer.images,
            latency_s=answer.latency_s,
            tokens_in=answer.tokens_in,
            tokens_out=answer.tokens_out,
            cost_usd=answer.usd,
            model=answer.model,
        )
