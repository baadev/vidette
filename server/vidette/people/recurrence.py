"""A stranger who keeps coming back: recurrence as a fact and as a policy rule (ADR-0014).

The anonymous identities (`vidette.people.identities`) reach a decision two ways:

1. **As facts for the judge.** The face check's report carries, for each unknown face
   matched to an identity, how often and where it was seen; `people_line` turns that
   into a sentence ("unknown face #12: this is the 3rd time today; cameras in the last
   hour: street, front_door"). The judge weighs it with what it sees. A cloud judge gets
   the counts and camera names, never a crop, an embedding or a name.
2. **As a policy rule** (`recurrence:` on a policy with levels): when the same unknown
   face was seen `sightings` times within `within`, or on `cameras` cameras within
   `cameras_within`, a verdict that named a scenario of a lower level is raised to
   `raise_to`. An identity someone marked suspicious raises on every sighting
   (`suspicious: true`); one marked ignored or promoted to a known person never does.

The rule is a nudge with a ceiling, not a verdict: it never turns `other` or an
abstention into an alert, never lowers a level, and the scenario the judge named stays
on the verdict, so a raised alert reads "passer_by, raised to elevated". Only behaviour
over time counts: how often and where, never what the person looks like.

`vidette eval` measures judges, so it does not apply the rule; the live worker does,
through `with_recurrence` around its judges.
"""

from __future__ import annotations

from collections.abc import Mapping

from vidette.core.config import PolicyConfig, RecurrenceConfig, VidetteConfig, Wake
from vidette.people.report import IdentityBrief, PeopleReport
from vidette.verdicts.base import Case, Judge, PolicyText, Verdict


def rule_met(rule: RecurrenceConfig, brief: IdentityBrief) -> str | None:
    """Why this identity meets the rule, in a few words; None when it does not."""
    if brief.status in ("ignored", "known"):
        return None
    if brief.status == "suspicious" and rule.suspicious:
        return "marked suspicious"
    seen = len(brief.within(rule.within_s))
    if seen >= rule.sightings:
        return f"{seen} sightings within {rule.within}"
    if rule.cameras is not None:
        cameras = brief.cameras_within(rule.cameras_within_s)
        if len(cameras) >= rule.cameras:
            return f"{len(cameras)} cameras within {rule.cameras_within}"
    return None


def recurring(
    policy: PolicyConfig, report: PeopleReport | None
) -> tuple[IdentityBrief, str] | None:
    """The first unknown identity in the report that meets the policy's rule, and why."""
    rule = policy.recurrence
    if rule is None or report is None:
        return None
    for seen in report.unknown:
        if seen.identity is None:
            continue
        why = rule_met(rule, seen.identity)
        if why is not None:
            return seen.identity, why
    return None


def raise_for_recurrence(
    policy: PolicyConfig, verdict: Verdict, report: PeopleReport | None
) -> Verdict:
    """The verdict raised to the rule's level when a recurring stranger was seen, or the
    verdict as it was."""
    rule = policy.recurrence
    if rule is None or verdict.abstained or verdict.level is None:
        return verdict
    names = list(policy.levels)
    if rule.raise_to not in names or verdict.level not in names:
        return verdict
    if names.index(verdict.level) <= names.index(rule.raise_to):
        return verdict  # already at or above it
    if recurring(policy, report) is None:
        return verdict
    level = policy.levels[rule.raise_to]
    return verdict.model_copy(
        update={"level": rule.raise_to, "alert": level.wake is not Wake.never}
    )


def was_raised(policy: PolicyConfig | None, verdict: Mapping[str, object]) -> bool:
    """Whether a stored verdict's level is higher than its scenario's own level (the
    recurrence rule raised it)."""
    if policy is None or not policy.levels:
        return False
    found = policy.scenario_level(str(verdict.get("label") or ""))
    level = verdict.get("level")
    return found is not None and isinstance(level, str) and found[0] != level


class RecurrenceJudge:
    """A judge that applies the policies' recurrence rules to another judge's verdicts."""

    def __init__(self, inner: Judge, policies: Mapping[str, PolicyConfig]) -> None:
        self._inner = inner
        self._policies = dict(policies)

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def description(self) -> str:
        return self._inner.description

    @property
    def inner(self) -> Judge:
        return self._inner

    async def judge(self, case: Case, policy: PolicyText) -> Verdict:
        verdict = await self._inner.judge(case, policy)
        config = self._policies.get(policy.name)
        if config is None:
            return verdict
        return raise_for_recurrence(config, verdict, case.people)


def with_recurrence(judge: Judge, config: VidetteConfig) -> Judge:
    """`judge` wrapped so the policies' recurrence rules apply, when any policy has one
    and unknown faces are tracked; `judge` itself otherwise."""
    people = config.people
    rules = {p.name: p for p in config.policies if p.recurrence is not None}
    if not rules or not (people.enabled and people.track_unknown):
        return judge
    return RecurrenceJudge(judge, rules)
