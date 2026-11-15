"""Incidents: what the upstream NVR saw, in Vidette's own words.

An *incident* is the unit a policy judges: one stretch of activity on one camera, as the
upstream NVR grouped it (a Frigate review item maps 1:1). A *tracked object* is the finer
evidence inside it (a Frigate event: one person, one car). Both are source-agnostic on
purpose (ADR-0009): Frigate is the first source, not the only possible one, and `vidette
eval` builds the same `Incident` from a labelled dataset so offline numbers and live
behaviour come from the same code path.

On the in-process bus they travel as JSON dicts (`model_dump(mode="json")`) under
`incident.<phase>` and `object.<phase>`; consumers rebuild them with `model_validate`.
These topics are internal: notification rules and the WebSocket feed only see `event.*`
and `system.*`, so an incident never notifies anyone before a verdict exists.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Phase(StrEnum):
    opened = "opened"  # first time Vidette hears of it (Frigate `new`, or first match)
    updated = "updated"  # more objects, zones, a severity upgrade
    closed = "closed"  # the activity ended upstream
    summarized = "summarized"  # the upstream attached its own GenAI summary (Frigate 0.17+)


class UpstreamSummary(BaseModel):
    """The upstream NVR's own description of an incident, kept for comparison.

    For Frigate this is the GenAI review metadata (`data.metadata`, Frigate 0.17+). It is
    the baseline Vidette has to beat, so `vidette eval` can score it as a judge.
    """

    title: str | None = None
    text: str | None = None  # Frigate `shortSummary`, falling back to `scene`
    threat_level: int | None = None  # Frigate `potential_threat_level`: 0, 1 or 2
    confidence: float | None = None
    concerns: list[str] = Field(default_factory=list)


class Incident(BaseModel):
    """One stretch of activity on one camera, as reported upstream."""

    source: str  # "frigate", "dataset", …
    source_id: str  # upstream id: the Frigate review id
    camera: str  # upstream camera key (Frigate's config key, not its friendly name)
    started_at: float  # unix epoch seconds
    ended_at: float | None = None
    severity: str | None = None  # upstream triage: Frigate "alert" | "detection"
    objects: list[str] = Field(default_factory=list)  # labels: person, car, cat, …
    sub_labels: list[str] = Field(default_factory=list)  # recognized faces, plates, …
    zones: list[str] = Field(default_factory=list)  # upstream zone names
    detections: list[str] = Field(default_factory=list)  # upstream tracked-object ids
    upstream_summary: UpstreamSummary | None = None
    # Local files the source already has (absolute paths): a clip folder's clip and key
    # frames. Empty for Frigate, whose media is fetched over its API.
    media: list[str] = Field(default_factory=list)
    # The early window of a recording still in progress (a clip folder's `.early/`): judged
    # at once so an alert need not wait; the whole clip follows under the same id.
    partial: bool = False
    # When each stage happened upstream (epoch seconds): the camera woke or the motion gate
    # opened, the first frame, the early window, the clip closed, the source picked it up.
    timeline: dict[str, float] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable identity across phases and restarts: `<source>:<source_id>`."""
        return f"{self.source}:{self.source_id}"

    @property
    def duration_s(self) -> float | None:
        return None if self.ended_at is None else max(0.0, self.ended_at - self.started_at)


class TrackedObject(BaseModel):
    """One tracked object inside an incident (a Frigate event)."""

    source: str
    source_id: str  # Frigate event id
    camera: str
    label: str
    sub_label: str | None = None
    score: float | None = None
    top_score: float | None = None
    started_at: float
    ended_at: float | None = None
    current_zones: list[str] = Field(default_factory=list)
    entered_zones: list[str] = Field(default_factory=list)
    stationary: bool = False
    motionless_count: int = 0
    position_changes: int = 0
    has_snapshot: bool = False
    box: list[float] | None = None  # pixels on the detect stream: x1, y1, x2, y2


def incident_topic(phase: Phase) -> str:
    return f"incident.{phase.value}"


def object_topic(phase: Phase) -> str:
    return f"object.{phase.value}"


def as_payload(model: BaseModel) -> dict[str, Any]:
    """Bus payloads are JSON-shaped by contract (they may be persisted or forwarded)."""
    return model.model_dump(mode="json")
