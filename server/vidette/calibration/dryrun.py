"""`vidette policy dry-run`: the last N days replayed through a policy, nothing sent.

Every event the database holds (Frigate review items and clip-folder clips alike) is
routed again from its stored verdict: the scenario the judge named, its confidence and
how many people it counted, the face check's report. What changes is the policy around
it: which level a scenario belongs to, who each level wakes, the sensitivity floor, and
calibration (ADR-0020):

    off      the policy exactly as written
    active   the calibration steps in force now, as if they had held all along
    replay   calibration replayed from the marks as they were given: no steps at the
             start, each 👍/👎 at its time, every step it would have made applied to
             the events after it

Each event ends up as `wake` (an alert someone is woken by), `digest` (confirmed, listed
in the evening digest), `silent` (the judge said ordinary, or below the floor) or
`unjudged` (no stored verdict: the queue never reached it, or the judge gave none).
Nothing is sent and no model is called, so a change of a scenario's *wording* is not
visible here (that needs the judge again, `vidette eval`); moving scenarios between
levels, `wake` rules, `sensitivity` and calibration are. Quiet hours are not replayed:
`/quiet` is per chat and per moment.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal

from vidette.calibration.rules import (
    STEP_WRITTEN,
    KeyState,
    Mark,
    Rules,
    effect,
    marks_from,
    propose,
    state_from,
)
from vidette.core.config import PolicyConfig, VidetteConfig, Wake
from vidette.db import CalibrationRow, EventRow
from vidette.eval.stored import Which, stored_verdicts
from vidette.telegram.routing import decide

Route = Literal["wake", "digest", "silent", "unjudged"]
Mode = Literal["off", "active", "replay"]
VerdictSource = Which
ROUTES: tuple[Route, ...] = ("wake", "digest", "silent", "unjudged")


@dataclass(frozen=True)
class Replayed:
    """One event, routed again."""

    event_id: str
    camera: str
    at: float
    day: str  # local date, YYYY-MM-DD
    scenario: str | None  # the judge's, None without a verdict
    route: Route  # with the chosen calibration
    written: Route  # the policy as written, no calibration
    step: int | None = None  # the calibration step that moved it (when it did)
    alerted: bool = False  # the bot really sent an alert about it
    feedback: str | None = None  # the 👍/👎 it got ("up" | "down")
    triage: str | None = None  # the pre-filter's class: now | later | skip (docs/prefilter.md)


@dataclass
class DryRun:
    since: float
    until: float
    mode: Mode
    verdicts: VerdictSource
    policies: list[str]
    events: list[Replayed] = field(default_factory=list)
    steps: list[CalibrationRow] = field(default_factory=list)  # the replayed or active ones

    def by_day(self) -> dict[str, Counter[str]]:
        days: dict[str, Counter[str]] = {}
        for event in self.events:
            row = days.setdefault(event.day, Counter())
            row["events"] += 1
            row[event.route] += 1
            row[f"written_{event.written}"] += 1
            row["alerted"] += int(event.alerted)
            row["marked_down"] += int(event.feedback == "down")
            row["marked_up"] += int(event.feedback == "up")
            if event.triage is not None:
                row[f"triage_{event.triage}"] += 1
        return dict(sorted(days.items()))

    @property
    def triaged(self) -> bool:
        """Whether the pre-filter looked at any of these events."""
        return any(event.triage is not None for event in self.events)

    def by_scenario(self) -> dict[tuple[str, str], Counter[str]]:
        rows: dict[tuple[str, str], Counter[str]] = {}
        for event in self.events:
            if event.scenario is None:
                continue
            row = rows.setdefault((event.scenario, event.camera), Counter())
            row[event.route] += 1
            row[f"written_{event.written}"] += 1
        return dict(sorted(rows.items(), key=lambda kv: (-kv[1]["written_wake"], kv[0])))

    def moved(self) -> list[Replayed]:
        """Events calibration moved from waking someone to the digest."""
        return [e for e in self.events if e.written == "wake" and e.route != "wake"]


@dataclass(frozen=True)
class Routed:
    route: Route
    scenario: str | None
    policy: PolicyConfig | None
    level: str | None
    confidence: float | None


def _level_of(policy: PolicyConfig, record: Mapping[str, Any]) -> str | None:
    """The scenario's level in this policy, or the level recurrence raised it to."""
    found = policy.scenario_level(str(record.get("label") or ""))
    if found is None:
        return None
    stored = record.get("level")
    rule = policy.recurrence
    names = list(policy.levels)
    if (
        rule is not None
        and stored == rule.raise_to
        and rule.raise_to in names
        and names.index(rule.raise_to) < names.index(found[0])
    ):
        return rule.raise_to
    return found[0]


def route_verdicts(
    records: Sequence[Mapping[str, Any]],
    policies: Sequence[PolicyConfig],
    people: Mapping[str, Any] | None,
) -> Routed:
    """Where an event goes under `policies` (those for its camera), from stored records:
    the first policy that alerts decides; without one, silent; without any decided
    record, unjudged."""
    decided = False
    for policy in policies:
        record = next((r for r in records if r.get("policy") == policy.name), None)
        if record is None and len(records) == 1 and len(policies) == 1:
            record = records[0]  # a renamed policy: the one verdict there is
        if record is None or record.get("abstained"):
            continue
        decided = True
        label = record.get("label")
        scenario = label if isinstance(label, str) else None
        raw = record.get("confidence")
        confidence = float(raw) if isinstance(raw, int | float) else None
        floor = policy.sensitivity.min_confidence
        sure = confidence is None or confidence >= floor
        if policy.levels:
            level = _level_of(policy, record)
            wake = Wake.never if level is None else policy.levels[level].wake
        else:
            level, wake = None, Wake.unless_quiet
            if not record.get("model_said_alert", record.get("alert")):
                wake = Wake.never
        if wake is Wake.never or not sure:
            continue
        persons = record.get("persons")
        route = decide(wake, people, persons if isinstance(persons, int) else None, quiet=False)
        return Routed("wake" if route == "alert" else "digest", scenario, policy, level, confidence)
    if not decided:
        return Routed("unjudged", None, None, None, None)
    first = next((r for r in records if not r.get("abstained")), {})
    label = first.get("label")
    return Routed("silent", label if isinstance(label, str) else None, None, None, None)


def _people(row: EventRow) -> Mapping[str, Any] | None:
    people = (row.intent or {}).get("people")
    return people if isinstance(people, Mapping) and "seen" in people else None


def _for_camera(config: VidetteConfig, camera: str) -> list[PolicyConfig]:
    return [p for p in config.policies if p.cameras == "all" or camera in p.cameras]


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts).date().isoformat()


def replay(
    rows: Iterable[EventRow],
    config: VidetteConfig,
    *,
    since: float,
    until: float,
    mode: Mode = "replay",
    steps: Sequence[CalibrationRow] = (),
    alerted: frozenset[str] | set[str] = frozenset(),
    verdicts: VerdictSource = "live",
) -> DryRun:
    """Route every event of [since, until) again. `rows` may start earlier: in `replay`
    mode the marks of the calibration window before `since` count, as they would have."""
    rules = Rules.from_config(config.calibration)
    policies = {p.name: p for p in config.policies}
    ordered = sorted(rows, key=lambda r: (r.started_at, r.id))
    run = DryRun(since, until, mode, verdicts, list(policies))
    simulated: list[CalibrationRow] = []
    if mode == "active":
        simulated = [row for row in steps if row.active]
    hidden: dict[str, int] = {}  # event → the simulated step that kept it from waking
    timeline: list[tuple[float, int, EventRow | Mark]] = [
        (row.ended_at or row.started_at, 0, row)
        for row in ordered
        if since <= row.started_at < until
    ]
    if mode == "replay" and config.calibration.enabled:
        timeline += [(mark.at, 1, mark) for mark in marks_from(ordered) if mark.at < until]
    timeline.sort(key=lambda item: (item[0], item[1]))
    marks: list[Mark] = []
    for at, _kind, item in timeline:
        if isinstance(item, Mark):
            marks.append(replace(item, hidden_by=hidden.get(item.event_id)))
            simulated = _apply(simulated, marks, rules, policies, at)
            continue
        states = state_from(simulated) if config.calibration.enabled else {}
        run.events.append(_route(item, config, states, rules, verdicts, alerted, hidden))
    run.steps = simulated if mode != "off" else []
    return run


def _route(
    row: EventRow,
    config: VidetteConfig,
    states: Mapping[tuple[str, str, str], KeyState],
    rules: Rules,
    verdicts: VerdictSource,
    alerted: frozenset[str] | set[str],
    hidden: dict[str, int],
) -> Replayed:
    routed = route_verdicts(
        stored_verdicts(row, verdicts), _for_camera(config, row.camera), _people(row)
    )
    route: Route = routed.route
    step: int | None = None
    if route == "wake" and routed.policy is not None and routed.scenario is not None:
        found = effect(
            states.get((routed.policy.name, routed.scenario, row.camera)),
            routed.policy,
            routed.scenario,
            level=routed.level,
            confidence=routed.confidence,
            rules=rules,
        )
        if found is not None:
            route, step = "digest", found.step_id
            hidden[row.id] = found.step_id
    triage = (row.intent or {}).get("triage")
    klass = triage.get("class") if isinstance(triage, dict) else None
    return Replayed(
        event_id=row.id,
        camera=row.camera,
        at=row.started_at,
        day=_day(row.started_at),
        scenario=routed.scenario,
        route=route,
        written=routed.route,
        step=step,
        alerted=row.id in alerted,
        feedback=row.feedback,
        triage=klass if klass in ("now", "later", "skip") else None,
    )


def _apply(
    simulated: list[CalibrationRow],
    marks: Sequence[Mark],
    rules: Rules,
    policies: Mapping[str, PolicyConfig],
    at: float,
) -> list[CalibrationRow]:
    """The steps after one more mark, as the service would have stored them."""
    rows = list(simulated)
    for proposal in propose(marks, state_from(rows), rules, policies, now=at):
        if proposal.undo is not None:
            target = next((r for r in rows if r.id == proposal.undo), None)
            if target is None:
                continue
            rows = [
                replace(r, reverted_at=at, reverted_by="mark")
                if r.key == target.key and r.id >= target.id and r.active
                else r
                for r in rows
            ]
            continue
        policy, scenario, camera = proposal.key
        rows.append(
            CalibrationRow(
                id=len(rows) + 1, at=at, policy=policy, scenario=scenario, camera=camera,
                step_from=proposal.step_from, step_to=proposal.step_to, bar=proposal.bar,
                evidence=proposal.evidence,
            )
        )  # fmt: skip
    return rows


# --- output ---------------------------------------------------------------------------------


def _how(row: CalibrationRow) -> str:
    if not row.active:
        return f"taken back ({row.reverted_by})"
    if row.step_to == STEP_WRITTEN:
        return "as written"
    if row.step_to >= 2:
        return "digest only"
    return f"wakes only from {round((row.bar or 0) * 100)} % sure"


def render_text(run: DryRun, *, camera_names: Mapping[str, str] | None = None) -> str:
    names = camera_names or {}
    cal = {
        "off": "off (the policy as written)",
        "active": f"the {len(run.steps)} step(s) in force now, as if they had held all along",
        "replay": "replayed from your marks as they were given",
    }[run.mode]
    source = {"live": "live verdicts", "deep": "deep-analysis verdicts",
              "best": "live verdicts, deep analysis where there is none"}[run.verdicts]  # fmt: skip
    lines = [
        f"Dry run: policies {', '.join(run.policies) or '(none)'}, "
        f"{datetime.fromtimestamp(run.since):%Y-%m-%d %H:%M} to "
        f"{datetime.fromtimestamp(run.until):%Y-%m-%d %H:%M}, stored {source}.",
        f"Calibration: {cal}.",
        "Nothing was sent and no model was called; quiet hours are not replayed.",
        "Marked: the 👍/👎 those days' events got (in Telegram or on the web page).",
        "",
        f"{'day':<12}{'events':>8}{'wake':>7}{'(written)':>11}{'digest':>8}{'silent':>8}"
        f"{'unjudged':>10}{'sent then':>11}{'marked 👎/👍':>13}",
    ]
    total: Counter[str] = Counter()
    for day, row in run.by_day().items():
        total.update(row)
        lines.append(_day_line(day, row))
    lines.append(_day_line("total", total))
    if run.triaged:
        lines += [
            "",
            "Pre-filter (docs/prefilter.md), events it looked at: "
            f"{total.get('triage_now', 0)} judged first, {total.get('triage_later', 0)} "
            f"when the model was idle, {total.get('triage_skip', 0)} skipped (nobody there); "
            "skipped events are among the unjudged.",
        ]
    moved = run.moved()
    lines += ["", f"Calibration moved {len(moved)} alert(s) to the digest."]
    if run.steps:
        lines.append("Steps:")
        for step in run.steps:
            camera = names.get(step.camera, step.camera)
            lines.append(
                f"  {datetime.fromtimestamp(step.at):%m-%d %H:%M}  {step.scenario} on {camera}: "
                f"{_how(step)} ({_evidence(step.evidence)})"
            )
    scenarios = run.by_scenario()
    if scenarios:
        lines += [
            "",
            f"{'scenario':<24}{'camera':<14}{'wake':>6}{'(written)':>11}{'digest':>8}{'silent':>8}",
        ]
        for (scenario, camera), row in scenarios.items():
            lines.append(
                f"{scenario[:23]:<24}{names.get(camera, camera)[:13]:<14}{row['wake']:>6}"
                f"{row['written_wake']:>11}{row['digest']:>8}{row['silent']:>8}"
            )
    return "\n".join(lines) + "\n"


def _day_line(day: str, row: Mapping[str, int]) -> str:
    return (
        f"{day:<12}{row.get('events', 0):>8}{row.get('wake', 0):>7}"
        f"{row.get('written_wake', 0):>11}{row.get('digest', 0):>8}{row.get('silent', 0):>8}"
        f"{row.get('unjudged', 0):>10}{row.get('alerted', 0):>11}"
        f"{str(row.get('marked_down', 0)) + '/' + str(row.get('marked_up', 0)):>13}"
    )


def _evidence(evidence: Mapping[str, Any]) -> str:
    if "down" not in evidence:
        return "a 👍 on one it had kept in the digest"
    reasons = evidence.get("reasons") or {}
    listed = (
        ", ".join(f"{'no reason' if k == 'none' else k} {v}" for k, v in reasons.items())
        if isinstance(reasons, dict)
        else ""
    )
    return f"{evidence['down']} × 👎: {listed}" if listed else f"{evidence['down']} × 👎"


def render_json(run: DryRun) -> dict[str, Any]:
    return {
        "since": run.since,
        "until": run.until,
        "calibration": run.mode,
        "verdicts": run.verdicts,
        "policies": run.policies,
        "days": {day: dict(row) for day, row in run.by_day().items()},
        "scenarios": [
            {"scenario": s, "camera": c, **dict(row)} for (s, c), row in run.by_scenario().items()
        ],
        "moved": [e.event_id for e in run.moved()],
        "steps": [
            {
                "id": r.id,
                "at": r.at,
                "policy": r.policy,
                "scenario": r.scenario,
                "camera": r.camera,
                "step_from": r.step_from,
                "step_to": r.step_to,
                "bar": r.bar,
                "evidence": r.evidence,
                "reverted_at": r.reverted_at,
                "reverted_by": r.reverted_by,
            }
            for r in run.steps
        ],
    }
