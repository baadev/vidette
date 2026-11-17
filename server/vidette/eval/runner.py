"""Run judges over a labelled dataset and score them (`vidette eval`)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace

from vidette.eval.dataset import Dataset, entry_day
from vidette.eval.metrics import Outcome, Score, score
from vidette.verdicts.base import Case, Judge, Verdict

# Turns an entry's case into the case the judge sees (the face check adds `people`).
Enrich = Callable[[str, Case], Awaitable[Case]]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JudgeRun:
    judge: str
    judge_description: str
    policy: str
    score: Score
    outcomes: list[Outcome]


async def run_judge(
    dataset: Dataset,
    policy_name: str,
    judge: Judge,
    *,
    enrich: Enrich | None = None,
    known_people: str | None = None,
) -> JudgeRun:
    """Judge every labelled entry once. A judge that raises abstains for that entry (and
    the failure is logged): one broken clip must not void a whole evaluation.

    `enrich` adds what the judge may know besides the frames (the face check);
    `known_people` overrides the policies' own setting for this run."""
    policy = dataset.policy_text(policy_name)
    if known_people is not None:
        policy = replace(policy, known_people=known_people)
    outcomes: list[Outcome] = []
    for entry in dataset.scored_entries:
        try:
            case = dataset.case(entry)
            if enrich is not None:
                case = await enrich(entry.id, case)
            verdict = await judge.judge(case, policy)
        except Exception as exc:
            logger.warning("judge %s failed on entry %s: %s", judge.name, entry.id, exc)
            verdict = Verdict(alert=False, abstained=True, rationale=f"judge failed: {exc}")
        outcomes.append(
            Outcome(
                entry_id=entry.id,
                scenario=entry.scenario,
                expected=dataset.expected_alert(entry, policy_name),
                alerted=verdict.alert,
                abstained=verdict.abstained,
                vlm_calls=verdict.vlm_calls,
                images_sent=verdict.images_sent,
                latency_s=verdict.latency_s,
                cost_usd=verdict.cost_usd,
                tokens_in=verdict.tokens_in,
                tokens_out=verdict.tokens_out,
                abstain_reason=verdict.rationale if verdict.abstained else None,
                day=entry_day(entry),
            )
        )
    return JudgeRun(
        judge=judge.name,
        judge_description=judge.description,
        policy=policy_name,
        score=score(outcomes, observed_hours=dataset.labels.observed_hours),
        outcomes=outcomes,
    )


async def run_eval(
    dataset: Dataset,
    policies: Sequence[str],
    judges: Sequence[Judge],
    *,
    enrich: Enrich | None = None,
    known_people: str | None = None,
) -> list[JudgeRun]:
    for name in policies:
        dataset.policy(name)  # fail before judging anything when a name is wrong
    return [
        await run_judge(dataset, name, judge, enrich=enrich, known_people=known_people)
        for name in policies
        for judge in judges
    ]
