"""Verdicts: does this incident match this policy?

`base` defines the one interface every judge implements, live and offline alike: the
week-2 VLM worker, the stub baselines below and anything that comes later. `baselines`
holds the judges that need no model, used by `vidette eval` to put numbers next to the
two things a person can already run today: "notify on everything" and Frigate's own
triage.
"""

from vidette.verdicts.base import Case, Judge, PolicyText, Verdict
from vidette.verdicts.baselines import BASELINES, baseline

__all__ = ["BASELINES", "Case", "Judge", "PolicyText", "Verdict", "baseline"]
