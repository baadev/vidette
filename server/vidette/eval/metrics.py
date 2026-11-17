"""Scoring verdicts against ground truth: counts, rates, intervals, per-scenario tables.

Small labelled sets are the norm here (30 to 50 clips), so every rate comes with a 95 %
Wilson score interval and its denominator. A precision of 1.00 on three alerts is not the
same claim as 1.00 on three hundred, and the report must not let anyone read it as one.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

Z_95 = 1.959963984540054


@dataclass(frozen=True)
class Rate:
    """k out of n, with a 95 % Wilson interval; `value` is None when n == 0."""

    k: int
    n: int

    @property
    def value(self) -> float | None:
        return None if self.n == 0 else self.k / self.n

    @property
    def interval(self) -> tuple[float, float] | None:
        return wilson_interval(self.k, self.n)


def wilson_interval(k: int, n: int, z: float = Z_95) -> tuple[float, float] | None:
    """Wilson score interval for a binomial proportion; None when n == 0."""
    if n == 0:
        return None
    if not 0 <= k <= n:
        raise ValueError(f"k={k} must be between 0 and n={n}")
    p = k / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, center - half), min(1.0, center + half)


@dataclass(frozen=True)
class Outcome:
    """One scored entry."""

    entry_id: str
    scenario: str
    expected: bool  # ground truth: should this policy alert?
    alerted: bool  # what the judge said
    abstained: bool = False
    vlm_calls: int = 0
    images_sent: int = 0
    latency_s: float | None = None  # measured on the call (VLM judges)
    cost_usd: float | None = None  # estimated from configured prices; None = unknown
    tokens_in: int | None = None
    tokens_out: int | None = None
    abstain_reason: str | None = None
    day: str | None = None  # the entry's local date (YYYY-MM-DD), when it has `at`


@dataclass
class Confusion:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    def add(self, outcome: Outcome) -> None:
        if outcome.alerted and outcome.expected:
            self.tp += 1
        elif outcome.alerted:
            self.fp += 1
        elif outcome.expected:
            self.fn += 1
        else:
            self.tn += 1

    @property
    def total(self) -> int:
        return self.tp + self.fp + self.fn + self.tn


@dataclass
class DayRow:
    """One calendar day of a run: what happened to that day's entries."""

    day: str
    entries: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    abstained: int = 0


@dataclass
class ScenarioRow:
    scenario: str
    expected: bool
    total: int = 0
    alerted: int = 0


@dataclass
class Score:
    """Everything the report prints for one (judge, policy) run."""

    confusion: Confusion
    observed_hours: float | None
    vlm_calls: int
    images_sent: int
    abstained: int
    by_scenario: list[ScenarioRow] = field(default_factory=list)
    false_alarms: list[str] = field(default_factory=list)  # entry ids
    misses: list[str] = field(default_factory=list)  # entry ids
    latencies: list[float] = field(default_factory=list)  # seconds, one per VLM verdict
    cost_usd: float | None = 0.0  # None when some call had no price (cloud, prices unset)
    tokens_in: int = 0
    tokens_out: int = 0
    abstain_reasons: dict[str, int] = field(default_factory=dict)
    by_day: list[DayRow] = field(default_factory=list)  # entries with `at`, oldest first

    def latency(self, quantile: float) -> float | None:
        """Nearest-rank quantile of the measured latencies (None without VLM calls)."""
        if not self.latencies:
            return None
        ordered = sorted(self.latencies)
        rank = max(1, math.ceil(quantile * len(ordered)))
        return ordered[rank - 1]

    @property
    def precision(self) -> Rate:
        return Rate(self.confusion.tp, self.confusion.tp + self.confusion.fp)

    @property
    def recall(self) -> Rate:
        return Rate(self.confusion.tp, self.confusion.tp + self.confusion.fn)

    @property
    def alerts(self) -> int:
        return self.confusion.tp + self.confusion.fp

    @property
    def observed_days(self) -> float | None:
        return None if self.observed_hours is None else self.observed_hours / 24.0

    def per_day(self, count: int) -> float | None:
        days = self.observed_days
        return None if days is None else count / days

    @property
    def false_alarms_per_day(self) -> float | None:
        return self.per_day(self.confusion.fp)

    @property
    def alerts_per_day(self) -> float | None:
        return self.per_day(self.alerts)


def score(outcomes: list[Outcome], *, observed_hours: float | None) -> Score:
    confusion = Confusion()
    rows: dict[str, ScenarioRow] = {}
    for outcome in outcomes:
        confusion.add(outcome)
        row = rows.setdefault(
            outcome.scenario, ScenarioRow(scenario=outcome.scenario, expected=outcome.expected)
        )
        row.total += 1
        row.alerted += int(outcome.alerted)
    order = Counter(outcome.scenario for outcome in outcomes)
    called = [o for o in outcomes if o.vlm_calls > 0]
    cost: float | None = 0.0
    for outcome in called:
        if outcome.cost_usd is None or cost is None:
            cost = None
        else:
            cost += outcome.cost_usd
    reasons: Counter[str] = Counter(
        (o.abstain_reason or "no reason given").split(":", 1)[0].strip()
        for o in outcomes
        if o.abstained
    )
    dates: dict[str, DayRow] = {}
    for outcome in outcomes:
        if outcome.day is None:
            continue
        on_day = dates.setdefault(outcome.day, DayRow(outcome.day))
        on_day.entries += 1
        on_day.tp += int(outcome.alerted and outcome.expected)
        on_day.fp += int(outcome.alerted and not outcome.expected)
        on_day.fn += int(outcome.expected and not outcome.alerted)
        on_day.abstained += int(outcome.abstained)
    return Score(
        by_day=[dates[date] for date in sorted(dates)],
        confusion=confusion,
        observed_hours=observed_hours,
        vlm_calls=sum(outcome.vlm_calls for outcome in outcomes),
        images_sent=sum(outcome.images_sent for outcome in outcomes),
        abstained=sum(1 for outcome in outcomes if outcome.abstained),
        # Should-alert scenarios first, then by frequency, then by name.
        by_scenario=sorted(
            rows.values(), key=lambda row: (not row.expected, -order[row.scenario], row.scenario)
        ),
        false_alarms=[o.entry_id for o in outcomes if o.alerted and not o.expected],
        misses=[o.entry_id for o in outcomes if o.expected and not o.alerted],
        latencies=[o.latency_s for o in outcomes if o.latency_s is not None],
        cost_usd=cost,
        tokens_in=sum(o.tokens_in or 0 for o in outcomes),
        tokens_out=sum(o.tokens_out or 0 for o in outcomes),
        abstain_reasons=dict(reasons),
    )


@dataclass(frozen=True)
class CostModel:
    """Prices for a VLM verdict, to turn call counts into money (all default to 0: local).

    `frames_per_call` is the plan for the week-2 worker (one call per incident with a few
    frames); it is used to project what a VLM judge would cost on this set's volume.
    """

    usd_per_call: float = 0.0
    usd_per_image: float = 0.0
    frames_per_call: int = 4

    def cost(self, calls: int, images: int) -> float:
        return calls * self.usd_per_call + images * self.usd_per_image

    def projected(self, incidents: int) -> tuple[int, int, float]:
        """(calls, images, usd) if every incident got one VLM call."""
        images = incidents * self.frames_per_call
        return incidents, images, self.cost(incidents, images)
