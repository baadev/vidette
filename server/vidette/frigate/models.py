"""Typed views of what Frigate publishes, and their mapping to Vidette's incident model.

Built against the payloads documented for Frigate 0.18 (`docs/integrations/mqtt.md` and the
OpenAPI spec in Frigate's repository) and the `genai` review message added in 0.17. Reviews
exist since Frigate 0.14; earlier versions are not supported. Details: docs/frigate.md.

Parsing is deliberately lenient, the opposite of Vidette's own config: Frigate adds fields
in minor releases, so unknown fields are ignored and optional ones default. What we cannot
do without (an id, a camera, a start time) still fails validation, and the source counts
and reports that instead of guessing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import AliasChoices, BaseModel, BeforeValidator, ConfigDict, Field

from vidette.core.incidents import Incident, Phase, TrackedObject, UpstreamSummary

SOURCE = "frigate"


def _to_epoch(value: object) -> object:
    """Frigate's MQTT gives epoch floats; its HTTP review schema says date-time. Take both."""
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    return value


Epoch = Annotated[float, BeforeValidator(_to_epoch)]


def _sub_label(value: object) -> object:
    """`sub_label` is null, a string, or `[name, score]` depending on version and source."""
    if isinstance(value, list | tuple):
        return str(value[0]) if value else None
    return value


def _none_as_empty(value: object) -> object:
    return [] if value is None else value


StrList = Annotated[list[str], BeforeValidator(_none_as_empty)]


class FrigateModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# --- reviews (<prefix>/reviews, GET /api/review) ---------------------------------------------


class ReviewMetadata(FrigateModel):
    """GenAI review summary (`data.metadata`, Frigate 0.17+)."""

    title: str | None = None
    scene: str | None = None
    short_summary: str | None = Field(
        default=None, validation_alias=AliasChoices("shortSummary", "short_summary")
    )
    confidence: float | None = None
    other_concerns: StrList = Field(default_factory=list)
    potential_threat_level: int | None = None


class ReviewData(FrigateModel):
    detections: StrList = Field(default_factory=list)  # Frigate event ids
    objects: StrList = Field(default_factory=list)
    sub_labels: StrList = Field(default_factory=list)
    zones: StrList = Field(default_factory=list)
    audio: StrList = Field(default_factory=list)
    metadata: ReviewMetadata | None = None


class FrigateReview(FrigateModel):
    id: str
    camera: str
    start_time: Epoch
    end_time: Epoch | None = None
    severity: str  # "alert" | "detection" today; kept open for future values
    thumb_path: str | None = None
    has_been_reviewed: bool | None = None
    data: ReviewData = Field(default_factory=ReviewData)


class FrigateReviewMessage(FrigateModel):
    type: str  # new | update | end | genai
    before: FrigateReview | None = None
    after: FrigateReview


# --- tracked objects (<prefix>/events) -------------------------------------------------------


class FrigateTrackedObject(FrigateModel):
    id: str
    camera: str
    label: str
    sub_label: Annotated[str | None, BeforeValidator(_sub_label)] = None
    frame_time: Epoch | None = None
    start_time: Epoch
    end_time: Epoch | None = None
    score: float | None = None
    top_score: float | None = None
    false_positive: bool = False
    box: list[float] | None = None
    current_zones: StrList = Field(default_factory=list)
    entered_zones: StrList = Field(default_factory=list)
    has_snapshot: bool = False
    has_clip: bool = False
    stationary: bool = False
    motionless_count: int = 0
    position_changes: int = 0
    attributes: dict[str, Any] = Field(default_factory=dict)


class FrigateEventMessage(FrigateModel):
    type: str  # new | update | end
    before: FrigateTrackedObject | None = None
    after: FrigateTrackedObject


# --- mapping to Vidette's model ---------------------------------------------------------------

_OBJECT_PHASES = {"new": Phase.opened, "update": Phase.updated, "end": Phase.closed}


def object_phase(message_type: str) -> Phase | None:
    """Phase for a `<prefix>/events` message type; None for types we do not know."""
    return _OBJECT_PHASES.get(message_type)


def to_summary(metadata: ReviewMetadata | None) -> UpstreamSummary | None:
    if metadata is None:
        return None
    return UpstreamSummary(
        title=metadata.title,
        text=metadata.short_summary or metadata.scene,
        threat_level=metadata.potential_threat_level,
        confidence=metadata.confidence,
        concerns=list(metadata.other_concerns),
    )


def to_incident(review: FrigateReview) -> Incident:
    return Incident(
        source=SOURCE,
        source_id=review.id,
        camera=review.camera,
        started_at=review.start_time,
        ended_at=review.end_time,
        severity=review.severity,
        objects=list(review.data.objects),
        sub_labels=list(review.data.sub_labels),
        zones=list(review.data.zones),
        detections=list(review.data.detections),
        upstream_summary=to_summary(review.data.metadata),
    )


def to_tracked_object(obj: FrigateTrackedObject) -> TrackedObject:
    return TrackedObject(
        source=SOURCE,
        source_id=obj.id,
        camera=obj.camera,
        label=obj.label,
        sub_label=obj.sub_label,
        score=obj.score,
        top_score=obj.top_score,
        started_at=obj.start_time,
        ended_at=obj.end_time,
        current_zones=list(obj.current_zones),
        entered_zones=list(obj.entered_zones),
        stationary=obj.stationary,
        motionless_count=obj.motionless_count,
        position_changes=obj.position_changes,
        has_snapshot=obj.has_snapshot,
        box=obj.box,
    )
