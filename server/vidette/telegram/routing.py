"""Who is woken for a verdict: the level's `wake` rule, the face check and quiet hours.

    always        → now, also in quiet hours and when only known people are seen
    unless_quiet  → now, except in quiet hours (then the digest)
    unless_known  → now, unless quiet, or the face check recognized everyone the judge saw
    never         → not an alert: the digest

"Recognized everyone" is deliberately narrow, so that a doubt wakes someone: at least one
known or familiar face, no other face, and the judge counted no more people than were
recognized. No face check, a face too small to tell, or a judge that did not count people
all mean "not known", which wakes (the fail-toward-alerting rule of ADR-0012).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from vidette.core.config import PolicyConfig, Wake

Route = Literal["alert", "digest"]


def _familiar(person: Mapping[str, Any]) -> bool:
    identity = person.get("identity")
    return isinstance(identity, Mapping) and identity.get("status") == "familiar"


def known_only(people: Mapping[str, Any] | None, persons: int | None) -> bool:
    """Everyone the judge counted was recognized as household (enrolled) or familiar
    (an anonymous identity the household marked so, ADR-0016), and no other face."""
    if people is None or persons is None:
        return False
    seen = [p for p in people.get("seen") or [] if isinstance(p, Mapping)]
    ours = [p for p in seen if p.get("status") == "known" or _familiar(p)]
    others = [p for p in seen if p not in ours]
    return bool(ours) and not others and persons <= len(ours)


def decide(
    wake: Wake, people: Mapping[str, Any] | None, persons: int | None, *, quiet: bool
) -> Route:
    if wake is Wake.never:
        return "digest"
    if wake is Wake.always:
        return "alert"
    if quiet:
        return "digest"
    if wake is Wake.unless_known and known_only(people, persons):
        return "digest"
    return "alert"


def level_rank(policy: PolicyConfig | None, level: str | None) -> int:
    """0 for a policy's first (highest) level; policies without levels rank below them."""
    if policy is None or not policy.levels or level is None:
        return 99
    names = list(policy.levels)
    return names.index(level) if level in names else 99


def alerting_verdict(intent: Mapping[str, Any] | None, policy: str | None) -> dict[str, Any]:
    """The verdict that confirmed the event (or the first one), from `intent.verdicts`."""
    verdicts = [v for v in (intent or {}).get("verdicts") or [] if isinstance(v, dict)]
    for verdict in verdicts:
        if verdict.get("policy") == policy and verdict.get("alert"):
            return verdict
    alerting = [v for v in verdicts if v.get("alert")]
    return (alerting or verdicts or [{}])[0]
