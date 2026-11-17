"""Calibration rules (ADR-0020): marks and the steps in force in, proposals out.

A key is one scenario of one policy on one camera. Its ladder:

    0  as written      the policy decides
    1  bar             an alert wakes only when the judge is at least `bar` sure;
                       below that it goes to the digest
    2  digest          every alert of it goes to the digest

One step quieter when, since the key's last change and within `window`, there are at
least `marks` counted 👎 and not one 👍. Counted: every reason except `stalled` and
`camera` (the recording or the camera, not the policy) and `ours` (telling the household
apart is the face check's job; a stricter bar would hide strangers too). When most of
them say "right, but not important" the bar is skipped: the judge saw right, a bar on
its confidence changes nothing, only the digest does.

Back: a 👍 on an event a step kept out of the alerts takes that step back at once. Slow
to quiet, quick to wake again: a mistake here should cost a few extra alerts, never a
missed one.

Protected scenarios (every scenario of a `wake: always` level, and the scenarios and
levels named in `calibration.protect`) get the bar at most, never above
`protected_bar`, and never leave the alerts. The limit holds where a step is applied
too, so a policy edit that protects a scenario later caps a step made before.

Everything here is pure: the service (vidette.calibration.service) and the dry run
(vidette.calibration.dryrun) feed it from the database.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from vidette.core.config import OTHER_SCENARIO, CalibrationConfig, PolicyConfig, Wake
from vidette.db import CalibrationRow, EventRow
from vidette.telegram.routing import alerting_verdict

STEP_WRITTEN, STEP_BAR, STEP_DIGEST = 0, 1, 2
NOT_COUNTED = frozenset({"stalled", "camera", "ours"})
NO_REASON = "none"  # a 👎 without a reason (an old alert, the web page, "No reason")
UNIMPORTANT = "unimportant"
MAX_EVIDENCE_EVENTS = 20

Key = tuple[str, str, str]  # policy, scenario, camera


@dataclass(frozen=True)
class Rules:
    marks: int = 4
    window_s: float = 14 * 86400.0
    bar: float = 0.9
    protected_bar: float = 0.75
    protect: frozenset[str] = frozenset()

    @classmethod
    def from_config(cls, config: CalibrationConfig) -> Rules:
        return cls(
            marks=config.marks,
            window_s=config.window_s,
            bar=config.bar,
            protected_bar=config.protected_bar,
            protect=frozenset(config.protect),
        )


@dataclass(frozen=True)
class Mark:
    """One 👍/👎 on a confirmed event, as calibration counts it."""

    event_id: str
    key: Key
    answer: Literal["up", "down"]
    at: float  # when it was marked (the event's end when nobody said)
    level: str | None = None  # the verdict's level (a raised one is not counted)
    reason: str | None = None
    confidence: float | None = None
    hidden_by: int | None = None  # the step that kept this event out of the alerts


@dataclass(frozen=True)
class KeyState:
    step: int = STEP_WRITTEN
    bar: float | None = None
    step_id: int | None = None  # the step in force
    since: float = 0.0  # the key's last change: a step, or a step taken back
    active: tuple[int, ...] = ()  # every step of the key still in force


@dataclass(frozen=True)
class Proposal:
    key: Key
    step_from: int
    step_to: int
    bar: float | None
    evidence: dict[str, Any] = field(default_factory=dict)
    undo: int | None = None  # take this step back (a 👍 on what it hid)


@dataclass(frozen=True)
class Effect:
    """What a step in force does to one alert: it goes to the digest."""

    step_id: int
    step: int
    bar: float | None

    def record(self) -> dict[str, Any]:
        """As kept on the event (`intent.calibration`)."""
        return {"step": self.step_id, "level": self.step, "bar": self.bar, "route": "digest"}


def state_from(rows: Iterable[CalibrationRow]) -> dict[Key, KeyState]:
    """The step in force for every key that ever had one (rows oldest first)."""
    by_key: dict[Key, list[CalibrationRow]] = {}
    for row in rows:
        by_key.setdefault(row.key, []).append(row)
    states: dict[Key, KeyState] = {}
    for key, steps in by_key.items():
        steps.sort(key=lambda row: row.id)
        since = max(max(row.at, row.reverted_at or 0.0) for row in steps)
        active = [row for row in steps if row.active]
        last = active[-1] if active else None
        states[key] = KeyState(
            step=last.step_to if last else STEP_WRITTEN,
            bar=last.bar if last else None,
            step_id=last.id if last else None,
            since=since,
            active=tuple(row.id for row in active),
        )
    return states


def protected(policy: PolicyConfig, scenario: str, protect: Collection[str]) -> bool:
    """A scenario that may never leave the alerts: a `wake: always` level's, or named in
    `calibration.protect` (itself or its level)."""
    found = policy.scenario_level(scenario)
    if found is None:
        return False
    level_name, level = found
    return level.wake is Wake.always or scenario in protect or level_name in protect


def raised(policy: PolicyConfig, scenario: str, level: str | None) -> bool:
    """A verdict whose level is not its scenario's own (the recurrence rule raised it):
    calibration leaves those alone, they are about who comes back, not what they did."""
    found = policy.scenario_level(scenario)
    return found is not None and level is not None and found[0] != level


def effect(
    state: KeyState | None,
    policy: PolicyConfig,
    scenario: str | None,
    *,
    level: str | None,
    confidence: float | None,
    rules: Rules,
) -> Effect | None:
    """Whether the step in force sends this alert to the digest instead of waking."""
    if state is None or state.step_id is None or state.step == STEP_WRITTEN or not scenario:
        return None
    if policy.wake_for(scenario) is Wake.never or raised(policy, scenario, level):
        return None
    if protected(policy, scenario, rules.protect):
        step = min(state.step, STEP_BAR)
        bar = min(state.bar or rules.protected_bar, rules.protected_bar)
    else:
        step = state.step
        bar = state.bar or rules.bar
    if step >= STEP_DIGEST:
        return Effect(state.step_id, STEP_DIGEST, None)
    if confidence is None or confidence >= bar:
        return None  # no number to compare with: wake (fail toward alerting)
    return Effect(state.step_id, STEP_BAR, bar)


def marks_from(rows: Iterable[EventRow]) -> list[Mark]:
    """The marks on confirmed events, from their rows (feedback, review, calibration)."""
    marks: list[Mark] = []
    for row in rows:
        if row.feedback not in ("up", "down") or row.state != "confirmed":
            continue
        verdict = alerting_verdict(row.intent, row.policy)
        scenario = verdict.get("label")
        policy = row.policy or verdict.get("policy")
        if not isinstance(scenario, str) or not isinstance(policy, str):
            continue
        if scenario == OTHER_SCENARIO:
            continue
        intent = row.intent or {}
        review = intent.get("review")
        review = review if isinstance(review, Mapping) else {}
        reason = review.get("reason")
        marked = review.get("at")
        hidden = intent.get("calibration")
        hidden_by = None
        if isinstance(hidden, Mapping) and hidden.get("route") == "digest":
            step = hidden.get("step")
            hidden_by = step if isinstance(step, int) else None
        confidence = verdict.get("confidence")
        level = verdict.get("level")
        marks.append(
            Mark(
                event_id=row.id,
                key=(policy, scenario, row.camera),
                answer="up" if row.feedback == "up" else "down",
                at=float(marked) if isinstance(marked, int | float) else (
                    row.ended_at or row.started_at
                ),
                level=level if isinstance(level, str) else None,
                reason=reason if isinstance(reason, str) else None,
                confidence=float(confidence) if isinstance(confidence, int | float) else None,
                hidden_by=hidden_by,
            )
        )  # fmt: skip
    return marks


def propose(
    marks: Sequence[Mark],
    states: Mapping[Key, KeyState],
    rules: Rules,
    policies: Mapping[str, PolicyConfig],
    *,
    now: float,
) -> list[Proposal]:
    """What the marks ask for now: steps taken back first, then one step quieter per key
    at most. Keys of a policy or scenario that no longer exists are left alone."""
    proposals: list[Proposal] = []
    undone: set[int] = set()
    for mark in marks:
        state = states.get(mark.key)
        if (
            mark.answer == "up"
            and mark.hidden_by is not None
            and state is not None
            and mark.hidden_by in state.active
            and mark.hidden_by not in undone
        ):
            undone.add(mark.hidden_by)
            proposals.append(
                Proposal(
                    key=mark.key, step_from=state.step, step_to=STEP_WRITTEN, bar=None,
                    evidence={"up": 1, "events": [mark.event_id]}, undo=mark.hidden_by,
                )
            )  # fmt: skip
    touched = {p.key for p in proposals}
    by_key: dict[Key, list[Mark]] = {}
    for mark in marks:
        by_key.setdefault(mark.key, []).append(mark)
    for key, key_marks in sorted(by_key.items()):
        if key in touched:
            continue
        proposal = _quieter(key, key_marks, states.get(key) or KeyState(), rules, policies, now)
        if proposal is not None:
            proposals.append(proposal)
    return proposals


def _quieter(
    key: Key,
    marks: Sequence[Mark],
    state: KeyState,
    rules: Rules,
    policies: Mapping[str, PolicyConfig],
    now: float,
) -> Proposal | None:
    policy = policies.get(key[0])
    scenario = key[1]
    if policy is None or scenario not in policy.scenario_configs():
        return None
    if policy.wake_for(scenario) is Wake.never:
        return None  # not an alert in the first place
    fresh = [
        m
        for m in marks
        if m.at > state.since and m.at > now - rules.window_s
        and not raised(policy, scenario, m.level)
    ]  # fmt: skip
    if any(m.answer == "up" for m in fresh):
        return None
    downs = [m for m in fresh if (m.reason or NO_REASON) not in NOT_COUNTED]
    if len(downs) < rules.marks:
        return None
    reasons = Counter(m.reason or NO_REASON for m in downs)
    floor = policy.sensitivity.min_confidence
    if protected(policy, scenario, rules.protect):
        if state.step >= STEP_BAR or rules.protected_bar <= floor:
            return None  # as strict as a protected scenario may get
        step_to, bar = STEP_BAR, rules.protected_bar
    else:
        if state.step >= STEP_DIGEST:
            return None
        unimportant = reasons[UNIMPORTANT] * 2 > len(downs)
        if state.step == STEP_WRITTEN and not unimportant and rules.bar > floor:
            step_to, bar = STEP_BAR, rules.bar
        else:
            step_to, bar = STEP_DIGEST, None
    return Proposal(
        key=key,
        step_from=state.step,
        step_to=step_to,
        bar=bar,
        evidence={
            "down": len(downs),
            "up": 0,
            "reasons": dict(reasons.most_common()),
            "events": [m.event_id for m in downs][-MAX_EVIDENCE_EVENTS:],
            "confidence": sorted(round(m.confidence, 2) for m in downs if m.confidence is not None),
        },
    )
