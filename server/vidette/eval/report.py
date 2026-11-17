"""Render `vidette eval` results as a text report or JSON.

The text report is written to be pasted into an issue or a README as is: what was
measured, on how much, with which uncertainty, and what it would cost with a VLM.
"""

from __future__ import annotations

import textwrap
from collections.abc import Sequence
from typing import Any

from vidette.eval.dataset import Dataset
from vidette.eval.metrics import CostModel, Rate
from vidette.eval.runner import JudgeRun

_MAX_IDS = 8


def _rate(rate: Rate, empty: str) -> str:
    if rate.value is None or rate.interval is None:
        return empty
    low, high = rate.interval
    return f"{rate.value:.2f} ({low:.2f}-{high:.2f})"


def _per_day(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}"


def _ids(ids: list[str]) -> str:
    shown = ", ".join(ids[:_MAX_IDS])
    return shown + (f" … (+{len(ids) - _MAX_IDS})" if len(ids) > _MAX_IDS else "")


def _usd(value: float) -> str:
    return f"${value:.4f}" if 0 < value < 0.01 else f"${value:.2f}"


def render_text(dataset: Dataset, runs: Sequence[JudgeRun], cost: CostModel) -> str:
    labels = dataset.labels
    scored = len(dataset.scored_entries)
    hours = (
        f"{labels.observed_hours:g} h observed"
        if labels.observed_hours is not None
        else "observed_hours not set, so no per-day numbers"
    )
    drafts = (
        f", {dataset.draft_count} machine drafts awaiting review (not scored)"
        if dataset.draft_count and not dataset.include_drafts
        else ""
    )
    lines = [
        f"Dataset  {labels.name}: {scored} entries scored, {dataset.unlabelled_count} "
        f"unlabelled{drafts}, {hours}",
        f"         license: {labels.license}",
    ]
    if dataset.include_drafts and dataset.draft_count:
        lines.append(
            f"         DRAFTS INCLUDED: {dataset.draft_count} truth labels are machine drafts "
            "nobody has checked; do not quote these numbers"
        )
    if dataset.accepted_unchanged_count:
        lines.append(
            f"         {dataset.accepted_unchanged_count} of {scored} truth labels are model "
            "drafts a reviewer accepted unchanged"
        )
    if labels.description:
        lines.append(f"         {labels.description.strip().splitlines()[0]}")
    if scored == 0:
        lines.append(
            "         nothing scored yet: give the unlabelled entries a scenario (docs/eval.md)"
        )

    for policy_name in dict.fromkeys(run.policy for run in runs):
        policy = dataset.policy(policy_name)
        policy_runs = [run for run in runs if run.policy == policy_name]
        should = sum(
            1 for entry in dataset.scored_entries if dataset.expected_alert(entry, policy_name)
        )
        lines += [
            "",
            textwrap.fill(
                f"Policy   {policy_name}: {' '.join(policy.description.split())}",
                width=92,
                subsequent_indent=" " * 9,
            ),
            f"Truth    {should} of {scored} entries should alert "
            f"(alert_on: {', '.join(policy.alert_on)})",
            "",
            f"{'judge':<18}{'alerts':>7}{'TP':>4}{'FP':>4}{'FN':>4}{'abst':>6}  "
            f"{'precision (95% CI)':<20}{'recall (95% CI)':<20}{'false alarms/day':>16}",
        ]
        for run in policy_runs:
            s, c = run.score, run.score.confusion
            lines.append(
                f"{run.judge:<18}{s.alerts:>7}{c.tp:>4}{c.fp:>4}{c.fn:>4}{s.abstained:>6}  "
                f"{_rate(s.precision, 'n/a, no alerts'):<20}"
                f"{_rate(s.recall, 'n/a, none due'):<20}"
                f"{_per_day(s.false_alarms_per_day):>16}"
            )

        scenarios = (
            [row.scenario for row in policy_runs[0].score.by_scenario] if policy_runs else []
        )
        if scenarios:
            width = max(len(name) for name in scenarios) + 2
            header = f"{'alerted / total':<{width}}{'expected':<10}" + "".join(
                f"{run.judge:>18}" for run in policy_runs
            )
            lines += ["", header]
            for scenario in scenarios:
                rows = [
                    next(row for row in run.score.by_scenario if row.scenario == scenario)
                    for run in policy_runs
                ]
                expected = "alert" if rows[0].expected else "silent"
                cells = "".join(f"{f'{row.alerted}/{row.total}':>18}" for row in rows)
                lines.append(f"{scenario:<{width}}{expected:<10}{cells}")

        dates = sorted({d.day for run in policy_runs for d in run.score.by_day})
        if len(dates) > 1:
            lines += [
                "",
                f"{'by day: FP / FN':<18}{'entries':>8}"
                + "".join(f"{run.judge:>18}" for run in policy_runs),
            ]
            for date in dates:
                found = [
                    next((d for d in run.score.by_day if d.day == date), None)
                    for run in policy_runs
                ]
                entries = max((d.entries for d in found if d is not None), default=0)
                cells = "".join(f"{'-' if d is None else f'{d.fp} / {d.fn}':>18}" for d in found)
                lines.append(f"{date:<18}{entries:>8}{cells}")

        abstained = [
            f"  {run.judge} abstained on {run.score.abstained} "
            f"entr{'y' if run.score.abstained == 1 else 'ies'} ({_reasons(run)}); "
            "an abstention counts as no alert"
            for run in policy_runs
            if run.score.abstained
        ]
        if abstained:
            lines += ["", *abstained]

        details = [
            f"  {run.judge}: false alarms [{_ids(run.score.false_alarms)}], "
            f"misses [{_ids(run.score.misses)}]"
            for run in policy_runs
            if run.score.false_alarms or run.score.misses
        ]
        if details:
            lines += ["", "Which entries", *details]

    measured = [run for run in runs if run.score.vlm_calls > 0]
    if measured:
        measured_days = measured[0].score.observed_days
        lines += [
            "",
            "Measured on this run (VLM judges; cost from your configured prices)",
            f"{'judge':<18}{'policy':<16}{'calls':>6}{'images':>8}{'tokens in/out':>16}"
            f"{'cost':>11}{'cost/day':>11}{'latency p50':>13}{'p95':>8}",
        ]
        for run in measured:
            sc = run.score
            cost_text = "unknown" if sc.cost_usd is None else _usd(sc.cost_usd)
            per_day_text = (
                "n/a"
                if sc.cost_usd is None or not measured_days
                else _usd(sc.cost_usd / measured_days)
            )
            lines.append(
                f"{run.judge:<18}{run.policy[:15]:<16}{sc.vlm_calls:>6}{sc.images_sent:>8}"
                f"{f'{sc.tokens_in}/{sc.tokens_out}':>16}{cost_text:>11}{per_day_text:>11}"
                f"{_seconds(sc.latency(0.5)):>13}{_seconds(sc.latency(0.95)):>8}"
            )
        if any(run.score.cost_usd is None for run in measured):
            lines.append(
                "  unknown: the backend has no prices configured "
                "(understanding.vlm.cloud.prices, docs/vlm.md)"
            )

    made_calls = sum(run.score.vlm_calls for run in runs)
    made_images = sum(run.score.images_sent for run in runs)
    calls, images, usd = cost.projected(scored)
    days = runs[0].score.observed_days if runs else None
    lines += ["", "VLM cost projection (priced with --usd-per-call / --usd-per-image)"]
    if not measured:
        lines.append(
            f"  made by these judges: {made_calls} calls, {made_images} images "
            f"({_usd(cost.cost(made_calls, made_images))})"
        )
    lines.append(
        f"  projected for a VLM judge (1 call per incident, {cost.frames_per_call} frames): "
        f"{calls} calls, {images} images, {_usd(usd)} on this set"
    )
    if days:
        per_day = usd / days
        lines.append(
            f"  at this set's volume: {calls / days:.1f} calls/day, {_usd(per_day)}/day, "
            f"{_usd(per_day * 30)} per 30 days"
        )
    lines.append(
        f"  prices: {_usd(cost.usd_per_call)}/call + {_usd(cost.usd_per_image)}/image"
        + (
            " (local model: $0 in API fees; pass --usd-per-image/--usd-per-call for a hosted one)"
            if cost.usd_per_call == 0 and cost.usd_per_image == 0
            else ""
        )
    )
    return "\n".join(lines) + "\n"


def _seconds(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f} s"


def _reasons(run: JudgeRun) -> str:
    reasons = run.score.abstain_reasons
    if not reasons:
        return "nothing to go on"
    return ", ".join(f"{reason} {count}" for reason, count in sorted(reasons.items()))


def _rate_json(rate: Rate) -> dict[str, Any]:
    return {"value": rate.value, "k": rate.k, "n": rate.n, "ci95": rate.interval}


def render_json(dataset: Dataset, runs: Sequence[JudgeRun], cost: CostModel) -> dict[str, Any]:
    labels = dataset.labels
    scored = len(dataset.scored_entries)
    calls, images, usd = cost.projected(scored)
    return {
        "dataset": {
            "name": labels.name,
            "root": str(dataset.root),
            "license": labels.license,
            "entries_scored": scored,
            "entries_unlabelled": dataset.unlabelled_count,
            "entries_draft": dataset.draft_count,
            "drafts_scored": dataset.include_drafts,
            "truth_from_accepted_drafts": dataset.accepted_unchanged_count,
            "observed_hours": labels.observed_hours,
        },
        "cost_model": {
            "usd_per_call": cost.usd_per_call,
            "usd_per_image": cost.usd_per_image,
            "frames_per_call": cost.frames_per_call,
        },
        "projection": {"calls": calls, "images": images, "usd": usd},
        "runs": [
            {
                "policy": run.policy,
                "judge": run.judge,
                "alerts": run.score.alerts,
                "tp": run.score.confusion.tp,
                "fp": run.score.confusion.fp,
                "fn": run.score.confusion.fn,
                "tn": run.score.confusion.tn,
                "abstained": run.score.abstained,
                "precision": _rate_json(run.score.precision),
                "recall": _rate_json(run.score.recall),
                "false_alarms_per_day": run.score.false_alarms_per_day,
                "alerts_per_day": run.score.alerts_per_day,
                "vlm": {
                    "calls": run.score.vlm_calls,
                    "images": run.score.images_sent,
                    "usd": cost.cost(run.score.vlm_calls, run.score.images_sent),
                },
                "by_scenario": [
                    {
                        "scenario": row.scenario,
                        "expected_alert": row.expected,
                        "alerted": row.alerted,
                        "total": row.total,
                    }
                    for row in run.score.by_scenario
                ],
                "false_alarms": run.score.false_alarms,
                "misses": run.score.misses,
                "by_day": [
                    {
                        "day": row.day,
                        "entries": row.entries,
                        "tp": row.tp,
                        "fp": row.fp,
                        "fn": row.fn,
                        "abstained": row.abstained,
                    }
                    for row in run.score.by_day
                ],
                "measured": {
                    "cost_usd": run.score.cost_usd,
                    "tokens_in": run.score.tokens_in,
                    "tokens_out": run.score.tokens_out,
                    "latency_s": {
                        "p50": run.score.latency(0.5),
                        "p95": run.score.latency(0.95),
                        "max": max(run.score.latencies, default=None),
                    },
                    "abstain_reasons": run.score.abstain_reasons,
                },
            }
            for run in runs
        ],
    }
