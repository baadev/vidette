"""The pre-filter as a `vidette eval` judge: what reaches the live judge at all.

- `triage`: "alerts" when the pre-filter would let a clip through (`now` or `later`), so
  its recall on a labelled set is the share of clips that should alert which the
  pre-filter would *not* have skipped. It must be 1.00.
- `triage-now`: "alerts" only for `now`: the share judged first.

Their precision means little (the pre-filter lets through far more than alerts); the
point is the misses. No model is called; the detector runs on this machine.
"""

from __future__ import annotations

from dataclasses import dataclass

from vidette.core.config import VidetteConfig
from vidette.triage.rules import TriageClass
from vidette.triage.service import Triager
from vidette.verdicts.base import Case, PolicyText, Verdict

TRIAGE_JUDGES = ("triage", "triage-now")


@dataclass
class TriageJudge:
    name: str
    triager: Triager

    @property
    def description(self) -> str:
        if self.name == "triage-now":
            return "the pre-filter would have the clip judged first (now)"
        return "the pre-filter would let the clip reach the judge (now or later)"

    @classmethod
    def from_config(cls, name: str, config: VidetteConfig) -> TriageJudge:
        settings = config.understanding.triage.model_copy(update={"usual_movement": False})
        return cls(name, Triager.from_config(settings, models_dir=config.people.models_dir))

    async def judge(self, case: Case, policy: PolicyText) -> Verdict:
        decision, observation = await self.triager.decide(case, camera=case.incident.camera)
        through = (
            decision.klass is TriageClass.now
            if self.name == "triage-now"
            else decision.klass is not TriageClass.skip
        )
        return Verdict(
            alert=through,
            abstained=False,
            label=decision.reason,
            rationale=f"{decision.klass.value}: {decision.why}",
            latency_s=observation.seconds,
        )
