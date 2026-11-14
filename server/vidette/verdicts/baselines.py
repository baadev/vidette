"""Stub judges that need no model: the baselines every real judge is compared against.

- `always`            alert on every incident: what "notify me on every Frigate review"
                      gives you, the false-alarm ceiling
- `never`             alert on nothing: the recall floor
- `frigate-severity`  alert when Frigate's own triage says `alert` (its alert rules,
                      usually "person or car in a zone")
- `frigate-genai`     alert when Frigate's GenAI review summary (0.17+) rates the
                      threat at 1 or more; abstains when there is no summary

None of them reads the policy text. That is the point: the week-2 VLM judge does, and
`vidette eval` shows whether reading it is worth its cost.
"""

from __future__ import annotations

from dataclasses import dataclass

from vidette.verdicts.base import Case, Judge, PolicyText, Verdict


@dataclass(frozen=True)
class AlwaysJudge:
    name: str = "always"
    description: str = "alert on every incident (notify on every Frigate review)"

    async def judge(self, case: Case, policy: PolicyText) -> Verdict:
        return Verdict(alert=True, rationale="baseline: every incident alerts")


@dataclass(frozen=True)
class NeverJudge:
    name: str = "never"
    description: str = "alert on nothing"

    async def judge(self, case: Case, policy: PolicyText) -> Verdict:
        return Verdict(alert=False, rationale="baseline: nothing alerts")


@dataclass(frozen=True)
class FrigateSeverityJudge:
    name: str = "frigate-severity"
    description: str = "alert when Frigate marks the review 'alert'"

    async def judge(self, case: Case, policy: PolicyText) -> Verdict:
        severity = case.incident.severity
        if severity is None:
            return Verdict(alert=False, abstained=True, rationale="no upstream severity")
        return Verdict(alert=severity == "alert", label=severity, rationale="Frigate severity")


@dataclass(frozen=True)
class FrigateGenAIJudge:
    name: str = "frigate-genai"
    description: str = "alert when Frigate's GenAI summary rates the threat 1 or 2"
    threshold: int = 1

    async def judge(self, case: Case, policy: PolicyText) -> Verdict:
        summary = case.incident.upstream_summary
        if summary is None or summary.threat_level is None:
            return Verdict(alert=False, abstained=True, rationale="no Frigate GenAI summary")
        return Verdict(
            alert=summary.threat_level >= self.threshold,
            label=f"threat_level_{summary.threat_level}",
            confidence=summary.confidence,
            rationale=summary.text,
        )


BASELINES: dict[str, Judge] = {
    judge.name: judge
    for judge in (AlwaysJudge(), NeverJudge(), FrigateSeverityJudge(), FrigateGenAIJudge())
}


def baseline(name: str) -> Judge:
    try:
        return BASELINES[name]
    except KeyError:
        known = ", ".join(sorted(BASELINES))
        raise ValueError(f"unknown judge '{name}'; available: {known}") from None
