"""The pre-filter as `vidette serve` runs it: one look per clip, a class, a memory.

`Triager.decide` looks at a case (vidette.triage.observe), classifies it
(vidette.triage.rules) and learns where things always move on that camera. The verdict
worker calls it for every incident before it waits for the judge, and keeps the
decision on the event (`intent.triage`). `warm` rebuilds the usual-movement memory from
those stored decisions after a restart, so the first hour after a restart is not
noisier than the rest.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vidette.core.config import TriageConfig
from vidette.db import EventRow
from vidette.triage.detector import DetectorUnavailableError, ObjectDetector, load_detector
from vidette.triage.observe import Observation, observe, unpack_cells
from vidette.triage.rules import Decision, TriageClass, UsualMovement, classify, teaches
from vidette.verdicts.base import Case

logger = logging.getLogger(__name__)


@dataclass
class TriageStatus:
    classes: Counter[str] = field(default_factory=Counter)  # now / later / skip
    reasons: Counter[str] = field(default_factory=Counter)
    seconds: float = 0.0  # time spent looking, in total
    errors: int = 0
    last_error: str | None = None

    @property
    def looked(self) -> int:
        return sum(self.classes.values())


class Triager:
    def __init__(
        self,
        config: TriageConfig,
        detector: ObjectDetector | None,
        *,
        error: str | None = None,
        work_dir: Path | None = None,
    ) -> None:
        self.config = config
        self.detector = detector
        self.error = error  # why there is no detector when one was asked for
        self.work_dir = work_dir
        self.memory = UsualMovement()
        self.status = TriageStatus()

    @classmethod
    def from_config(
        cls, config: TriageConfig, *, models_dir: Path | None = None, work_dir: Path | None = None
    ) -> Triager:
        """Never downloads (`vidette triage models` does): a missing model leaves the
        pre-filter on without a detector, which orders clips but skips none."""
        detector: ObjectDetector | None = None
        error: str | None = None
        if config.detector == "yolox":
            try:
                detector = load_detector(
                    config.models_dir or models_dir, download=False, threads=config.threads
                )
            except DetectorUnavailableError as exc:
                error = str(exc)
        return cls(config, detector, error=error, work_dir=work_dir)

    async def decide(
        self, case: Case, *, camera: str, partial: bool = False
    ) -> tuple[Decision, Observation]:
        observation = await observe(
            case,
            self.detector,
            frames=self.config.frames,
            key_frames=self.config.key_frames,
            work_dir=self.work_dir,
        )
        if self.detector is None and self.error and observation.error is None:
            observation.error = f"no detector: {self.error}"[:200]
        usual = self.memory.usual(camera) if self.config.usual_movement else None
        decision = classify(observation, self.config, partial=partial, usual=usual)
        if not partial and observation.checked and teaches(decision):
            self.memory.learn(camera, observation.cell_set())
        self.status.classes[decision.klass.value] += 1
        self.status.reasons[decision.reason] += 1
        self.status.seconds += observation.seconds
        if observation.error is not None:
            self.status.errors += 1
            self.status.last_error = observation.error
        return decision, observation

    def warm(self, rows: Iterable[EventRow]) -> int:
        """Learn the usual movement from decisions stored on events (oldest first)."""
        learned = 0
        for row in sorted(rows, key=lambda r: r.started_at):
            record = (row.intent or {}).get("triage")
            if not isinstance(record, dict) or record.get("phase") != "final":
                continue
            observed: Any = record.get("observed") or {}
            if record.get("reason") not in LEARNED or not isinstance(observed, dict):
                continue
            if observed.get("error"):
                continue
            self.memory.learn(row.camera, unpack_cells(observed.get("cells")))
            learned += 1
        return learned

    def line(self) -> dict[str, int]:
        return {k.value: self.status.classes.get(k.value, 0) for k in TriageClass}


LEARNED = frozenset({"light", "tiny", "usual", "motion", "few_frames", "nobody"})

__all__ = ["DetectorUnavailableError", "TriageStatus", "Triager"]
