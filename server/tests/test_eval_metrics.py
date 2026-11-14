"""Metric arithmetic for `vidette eval`: confusion counts, rates, intervals, per-day, cost."""

from __future__ import annotations

import pytest

from vidette.eval.metrics import (
    Confusion,
    CostModel,
    Outcome,
    Rate,
    score,
    wilson_interval,
)


def outcome(
    entry_id: str, scenario: str, *, expected: bool, alerted: bool, **kw: object
) -> Outcome:
    return Outcome(
        entry_id=entry_id,
        scenario=scenario,
        expected=expected,
        alerted=alerted,
        **kw,  # type: ignore[arg-type]
    )


OUTCOMES = [
    outcome("a", "door_handle_attempt", expected=True, alerted=True),  # TP
    outcome("b", "door_handle_attempt", expected=True, alerted=False),  # FN
    outcome("c", "lingering_at_door", expected=True, alerted=True),  # TP
    outcome("d", "passer_by", expected=False, alerted=True),  # FP
    outcome("e", "passer_by", expected=False, alerted=False),  # TN
    outcome("f", "passer_by", expected=False, alerted=False, abstained=True),  # TN, abstained
    outcome("g", "cat", expected=False, alerted=True, vlm_calls=1, images_sent=4),  # FP
]


def test_confusion_counts() -> None:
    confusion = Confusion()
    for item in OUTCOMES:
        confusion.add(item)
    assert (confusion.tp, confusion.fp, confusion.fn, confusion.tn) == (2, 2, 1, 2)
    assert confusion.total == 7


def test_precision_recall_and_per_day() -> None:
    result = score(OUTCOMES, observed_hours=48)
    assert result.precision == Rate(2, 4) and result.precision.value == 0.5
    assert result.recall == Rate(2, 3)
    assert result.recall.value == pytest.approx(2 / 3)
    assert result.alerts == 4
    assert result.false_alarms_per_day == pytest.approx(1.0)  # 2 FP over 2 days
    assert result.alerts_per_day == pytest.approx(2.0)
    assert result.abstained == 1
    assert (result.vlm_calls, result.images_sent) == (1, 4)
    assert result.false_alarms == ["d", "g"]
    assert result.misses == ["b"]


def test_per_scenario_rows_put_should_alert_scenarios_first() -> None:
    rows = score(OUTCOMES, observed_hours=None).by_scenario
    assert [(r.scenario, r.expected, r.alerted, r.total) for r in rows] == [
        ("door_handle_attempt", True, 1, 2),
        ("lingering_at_door", True, 1, 1),
        ("passer_by", False, 1, 3),
        ("cat", False, 1, 1),
    ]


def test_no_observed_hours_means_no_per_day_numbers() -> None:
    result = score(OUTCOMES, observed_hours=None)
    assert result.false_alarms_per_day is None
    assert result.alerts_per_day is None


def test_rates_without_a_denominator_are_none_not_zero() -> None:
    silent = score([outcome("x", "cat", expected=False, alerted=False)], observed_hours=24)
    assert silent.precision.value is None  # no alerts: precision is undefined
    assert silent.recall.value is None  # nothing was due: recall is undefined
    assert silent.precision.interval is None
    assert silent.false_alarms_per_day == 0.0


@pytest.mark.parametrize(
    ("k", "n", "low", "high"),
    [
        (0, 10, 0.0, 0.2775),
        (5, 10, 0.2366, 0.7634),
        (10, 10, 0.7225, 1.0),
        (1, 1, 0.2065, 1.0),
        (81, 263, 0.2553, 0.3662),
    ],
)
def test_wilson_interval_matches_reference_values(k: int, n: int, low: float, high: float) -> None:
    interval = wilson_interval(k, n)
    assert interval is not None
    assert interval[0] == pytest.approx(low, abs=5e-4)
    assert interval[1] == pytest.approx(high, abs=5e-4)


def test_wilson_interval_edges() -> None:
    assert wilson_interval(0, 0) is None
    with pytest.raises(ValueError):
        wilson_interval(3, 2)


def test_cost_model() -> None:
    cost = CostModel(usd_per_call=0.01, usd_per_image=0.002, frames_per_call=4)
    assert cost.cost(calls=3, images=12) == pytest.approx(0.054)
    assert cost.projected(incidents=10) == (10, 40, pytest.approx(0.18))
    assert CostModel().projected(7) == (7, 28, 0.0)
