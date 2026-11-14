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

from vidette.core.incidents import Incident
from vidette.people.report import PeopleReport


@dataclass(frozen=True)
class PolicyText:
    """What the judge is told: the policy's name and its plain-language description.

    `known_people`: "quiet" adds the rule that people recognized as known never need an
    alert by themselves (docs/people.md); "judge" (default) only reports who was seen."""

    name: str
    description: str
    known_people: str = "judge"


@dataclass(frozen=True)
class Case:
    """One incident to judge, with whatever media is at hand (frames or clips on disk),
    and the local face check's report when known people are enabled."""

    incident: Incident
    media: tuple[Path, ...] = field(default=())
    people: PeopleReport | None = None


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
