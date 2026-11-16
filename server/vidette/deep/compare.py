"""The live verdict against the deep one, in four plain categories (ADR-0019).

- `agree`: both decided, the same alert decision and the same level (the scenario may
  differ inside a level: `passer_by` and `car_use` are both "nothing to wake for");
- `disputed`: the alert decision or the level differs, or the live judge never decided
  (no verdict, not reached) and the deep one says alert;
- `uncertain`: either side abstained, or one of them is below `min_confidence`;
- `only_deep`: no live verdict and a confident deep "nothing to wake for".

Disputed and uncertain clips go to the next labelling batch: what a person reviews is
where the two judges part, not every clip of the day.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

Agreement = Literal["agree", "disputed", "uncertain", "only_deep"]
REVIEW: frozenset[str] = frozenset({"disputed", "uncertain"})


@dataclass(frozen=True)
class Side:
    """One judge's verdict on one policy, as stored (`verdict_record`)."""

    decided: bool
    alert: bool
    level: str | None
    scenario: str | None
    confidence: float | None

    @classmethod
    def of(cls, record: Mapping[str, Any] | None) -> Side:
        if not record:
            return cls(False, False, None, None, None)
        confidence = record.get("confidence")
        return cls(
            decided=not record.get("abstained"),
            alert=bool(record.get("alert")),
            level=record.get("level") if isinstance(record.get("level"), str) else None,
            scenario=record.get("label") if isinstance(record.get("label"), str) else None,
            confidence=float(confidence) if isinstance(confidence, int | float) else None,
        )


def compare(live: Side, deep: Side, *, min_confidence: float) -> tuple[Agreement, str]:
    """The category and why, in a few words."""
    if not deep.decided:
        return "uncertain", "the deep look could not decide"
    if not live.decided:
        if deep.alert:
            return "disputed", "no live verdict; the deep look says alert"
        if deep.confidence is not None and deep.confidence < min_confidence:
            return "uncertain", "no live verdict; the deep look is unsure"
        return "only_deep", "no live verdict; nothing to wake for"
    if live.alert != deep.alert:
        said = "alert" if deep.alert else "no alert"
        return "disputed", f"live {'alert' if live.alert else 'no alert'}, deep {said}"
    if live.level != deep.level:
        return "disputed", f"levels differ: live {live.level}, deep {deep.level}"
    low = [
        name
        for name, side in (("live", live), ("deep", deep))
        if side.confidence is not None and side.confidence < min_confidence
    ]
    if low:
        return "uncertain", f"{' and '.join(low)} unsure"
    return "agree", "same decision and level"


def live_side(intent: Mapping[str, Any] | None, policy: str) -> Side:
    """The live verdict of `policy` in an event's `intent`, or an undecided side."""
    for record in (intent or {}).get("verdicts") or []:
        if isinstance(record, Mapping) and record.get("policy") == policy:
            return Side.of(record)
    return Side.of(None)


def worst(categories: list[Agreement]) -> Agreement:
    """An event with several policies: the category that needs a person most."""
    for category in ("disputed", "uncertain", "only_deep", "agree"):
        if category in categories:
            return category
    return "agree"
