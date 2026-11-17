"""The judge interface: an incident, a policy in plain language, a structured verdict.

A judge sees what a person would see (the incident facts from upstream and its media)
and the policy text a person wrote. It never sees the evaluation's ground truth: the
`alert_on` list of a dataset policy stays in `vidette.eval`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from vidette.core.config import PolicyConfig, Wake
from vidette.core.incidents import Incident
from vidette.people.report import PeopleReport
from vidette.streamhealth import StreamHealth


@dataclass(frozen=True)
class LevelText:
    """One level of a policy as the judge hears it: its scenarios and whether they alert."""

    name: str
    alerts: bool
    scenarios: tuple[tuple[str, str], ...]  # (scenario id, what it means)


@dataclass(frozen=True)
class PolicyText:
    """What the judge is told: the policy's name and its plain-language description.

    `known_people`: "quiet" adds the rule that people recognized as known never need an
    alert by themselves (docs/people.md); "judge" (default) only reports who was seen.
    `levels`: with levels the judge names one scenario (or "other") instead of answering
    alert / silent, and the scenario's level decides whether it is an alert."""

    name: str
    description: str
    known_people: str = "judge"
    levels: tuple[LevelText, ...] = ()

    def scenario_level(self, scenario: str | None) -> LevelText | None:
        return next(
            (level for level in self.levels if any(s == scenario for s, _ in level.scenarios)),
            None,
        )


def policy_text(policy: PolicyConfig) -> PolicyText:
    """A config policy as the judge hears it (live and in tools alike)."""
    levels = tuple(
        LevelText(
            name=name,
            alerts=level.wake is not Wake.never,
            scenarios=tuple(
                (sid, scenario.description) for sid, scenario in level.scenario_configs().items()
            ),
        )
        for name, level in policy.levels.items()
    )
    return PolicyText(policy.name, policy.description, policy.known_people, levels)


@dataclass(frozen=True)
class FrameSet:
    """The frames chosen for one case (vidette.verdicts.frames): files on disk, their
    times in the clip (None when unknown), how they were chosen, and the clip's stream
    health when there was a clip."""

    paths: list[Path]
    times: list[float | None] = field(default_factory=list)
    method: str = "images"  # "activity", "spread" (a video, nothing moved), "images",
    # or "person" (a window around a person, vidette.verdicts.windows)
    duration_s: float | None = None
    health: StreamHealth | None = None
    note: str | None = None  # told to the judge after the frame times (a window's place)

    def timed(self) -> list[tuple[Path, float | None]]:
        times = self.times if len(self.times) == len(self.paths) else [None] * len(self.paths)
        return list(zip(self.paths, times, strict=True))


@dataclass(frozen=True)
class Case:
    """One incident to judge, with whatever media is at hand (frames or clips on disk),
    and the local face check's report when known people are enabled. `frames`: frames
    already chosen for it (the verdict worker chooses once for every policy)."""

    incident: Incident
    media: tuple[Path, ...] = field(default=())
    people: PeopleReport | None = None
    frames: FrameSet | None = None
    context: str | None = None  # one line about an alert minutes before (incident mode)


class Verdict(BaseModel):
    alert: bool
    abstained: bool = False  # the judge had nothing to go on; counted as "no alert"
    label: str | None = None  # e.g. "entry_attempt", "delivery", "passer_by"
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    rationale: str | None = None
    vlm_calls: int = Field(default=0, ge=0)  # model calls actually made for this verdict
    images_sent: int = Field(default=0, ge=0)
    # Measured on the call (VLM judges only): wall time, tokens, and the cost estimated
    # from configured prices. `cost_usd` None means unknown (no prices), not free.
    latency_s: float | None = Field(default=None, ge=0)
    tokens_in: int | None = Field(default=None, ge=0)
    tokens_out: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    model: str | None = None
    # Policies with levels: the level of the scenario named in `label` (None for "other")
    # and how many different people the judge saw (None when it did not say).
    level: str | None = None
    persons: int | None = Field(default=None, ge=0)
    # When a person is first in view, seconds into the clip (the judge names the frame).
    person_at_s: float | None = Field(default=None, ge=0)
    # the times of the frames that show what the verdict is about, clearest first: the
    # alert's album shows these, not frames of an empty street (ADR-0017)
    subject_at_s: list[float] = Field(default_factory=list)


class Judge(Protocol):
    """Anything that turns (case, policy) into a verdict.

    `name` is the stable id used on the command line and in reports. `judge` must not
    raise for ordinary bad input: return an abstaining verdict with a rationale instead.
    """

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    async def judge(self, case: Case, policy: PolicyText) -> Verdict: ...
