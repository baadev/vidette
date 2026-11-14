"""Machine drafts: a VLM picks a scenario from the dataset's own vocabulary.

Unlike the judge (which reads a policy and answers alert/silent), the labeller answers
"which of these things happened", so its draft is a proposed *fact* about the clip. Whether
that fact alerts is then decided the same way as for hand labels: by the policies'
`alert_on`. Drafts are never ground truth until a person reviews them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from vidette.eval.dataset import DraftInfo, Labels
from vidette.ffmpeg import MediaToolError
from vidette.verdicts.base import Case
from vidette.verdicts.frames import frame_jpegs
from vidette.verdicts.vlm import case_facts
from vidette.vlm.client import VlmClient, VlmError

UNCLEAR = "unclear"

SYSTEM_PROMPT = (
    "You label short clips from a home security camera to build an evaluation set. You get "
    "a few frames from one clip, in time order, and a list of scenarios. Pick the one "
    "scenario that best describes what happens in the clip. Judge only what the frames "
    "show. Night frames are often grey infrared images. If none fits or the frames do not "
    f'show enough, answer "{UNCLEAR}".'
)


def label_schema(scenarios: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "scenario": {"type": "string", "enum": [*scenarios, UNCLEAR]},
            "description": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["scenario", "description", "confidence"],
        "additionalProperties": False,
    }


@dataclass(frozen=True)
class DraftResult:
    draft: DraftInfo
    cost_usd: float | None
    latency_s: float


class DraftError(Exception):
    """The labeller could not run; `fatal` means the next clip would fail the same way."""

    def __init__(self, message: str, *, fatal: bool) -> None:
        super().__init__(message)
        self.fatal = fatal


def first_policy_alert(labels: Labels, scenario: str) -> bool | None:
    if scenario == UNCLEAR or not labels.policies:
        return None
    policy = next(iter(labels.policies.values()))
    return scenario in policy.alert_on


async def draft_label(
    client: VlmClient, labels: Labels, case: Case, *, frames: int, judge_name: str
) -> DraftResult:
    endpoint = client.endpoint
    try:
        images = await frame_jpegs(case, frames, max_side=endpoint.max_image_side)
    except (MediaToolError, OSError) as exc:
        raise DraftError(f"no frames: {exc}", fatal=False) from exc
    if not images:
        raise DraftError("no frames to look at", fatal=False)
    vocabulary = "\n".join(f"- {name}: {meaning}" for name, meaning in labels.scenarios.items())
    text = (
        f"Scenarios:\n{vocabulary}\n- {UNCLEAR}: none of the above, or impossible to tell\n\n"
        f"{case_facts(case, len(images), backend=endpoint.backend)}\n\n"
        "Answer with JSON: scenario (one name from the list), description (one sentence "
        "about what is visible), confidence (0 to 1)."
    )
    try:
        answer = await client.ask(
            system=SYSTEM_PROMPT,
            text=text,
            images=images,
            schema=label_schema(list(labels.scenarios)),
            schema_name="draft_label",
        )
    except VlmError as exc:
        fatal = exc.kind in ("budget", "network", "auth", "not_found")
        raise DraftError(f"{exc.kind}: {exc}", fatal=fatal) from exc
    scenario = str(answer.data.get("scenario", UNCLEAR))
    if scenario not in labels.scenarios:
        scenario = UNCLEAR
    confidence = answer.data.get("confidence")
    description = answer.data.get("description")
    return DraftResult(
        draft=DraftInfo(
            by=f"{judge_name} {answer.model}",
            at=datetime.now().astimezone().replace(microsecond=0),
            scenario=scenario,
            alert=first_policy_alert(labels, scenario),
            description=" ".join(str(description).split())[:200] if description else None,
            confidence=(
                min(1.0, max(0.0, float(confidence)))
                if isinstance(confidence, int | float)
                else None
            ),
        ),
        cost_usd=answer.usd,
        latency_s=answer.latency_s,
    )
