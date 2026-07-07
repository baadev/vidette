"""Retention planning — pure logic, no IO, fully tested.

Semantics (docs/architecture/storage.md#retention-classes):
- a segment is retained by its *highest* class: an event segment never dies on the
  continuous schedule;
- favorites are immortal;
- under disk pressure, oldest `continuous` goes first, then oldest `motion`;
  `event` (within retention) and `favorite` segments are NEVER pressure-deleted — if
  pressure remains, the plan reports `unmet_bytes` so the caller raises a loud
  `storage.pressure` system event instead of eating evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from vidette.core.config import Retention


class SegmentClass(StrEnum):
    continuous = "continuous"
    motion = "motion"
    event = "event"
    favorite = "favorite"


_PRESSURE_DELETABLE = (SegmentClass.continuous, SegmentClass.motion)


@dataclass(frozen=True)
class Segment:
    camera: str
    start: datetime
    end: datetime
    path: str
    size_bytes: int
    klass: SegmentClass


@dataclass
class DeletionPlan:
    expired: list[Segment] = field(default_factory=list)
    pressure: list[Segment] = field(default_factory=list)
    unmet_bytes: int = 0  # > 0 means: disk too small for the policy — raise a loud event

    @property
    def freed_bytes(self) -> int:
        return sum(s.size_bytes for s in self.expired) + sum(s.size_bytes for s in self.pressure)


def _limits(retention: Retention) -> dict[SegmentClass, timedelta | None]:
    return {
        SegmentClass.continuous: retention.continuous,
        SegmentClass.motion: retention.motion,
        SegmentClass.event: retention.events,
        SegmentClass.favorite: retention.favorites,
    }


def plan_deletions(
    segments: list[Segment],
    retention: Retention,
    *,
    now: datetime,
    bytes_to_free: int = 0,
) -> DeletionPlan:
    """Compute what to delete: class expiry first, then (if needed) pressure deletions."""
    limits = _limits(retention)
    plan = DeletionPlan()
    survivors: list[Segment] = []

    for segment in segments:
        limit = limits[segment.klass]
        if segment.klass is not SegmentClass.favorite and limit is not None and (
            now - segment.end > limit
        ):
            plan.expired.append(segment)
        else:
            survivors.append(segment)

    remaining = bytes_to_free - sum(s.size_bytes for s in plan.expired)
    if remaining > 0:
        deletable = sorted(
            (s for s in survivors if s.klass in _PRESSURE_DELETABLE),
            key=lambda s: (_PRESSURE_DELETABLE.index(s.klass), s.start),
        )
        for segment in deletable:
            if remaining <= 0:
                break
            plan.pressure.append(segment)
            remaining -= segment.size_bytes
        plan.unmet_bytes = max(0, remaining)

    return plan
