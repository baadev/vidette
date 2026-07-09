"""Cascade tier protocols — the typed seams of the AI pipeline (ADR-0003).

Each tier is independently replaceable and testable; the orchestrator (M2) wires them under
a CascadeBudget and the shedding ladder (docs/architecture/overview.md): T3 sheds first,
then T1–T2 fps, then previews — the recorder never sheds.

`image` parameters are typed as `object` on purpose at M0: the pixel-buffer type (numpy)
becomes a dependency only when the first real tier lands in M2 — the core stays light until
then, and the seams are what this module pins down.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class BBox:
    """Normalized (0..1) box: x, y is the top-left corner."""

    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class MotionRegion:
    bbox: BBox
    score: float  # 0..1 — fraction of changed pixels, damped


@dataclass(frozen=True)
class Detection:
    label: str  # person | vehicle | animal | package (M2 class list, deliberately short)
    confidence: float
    bbox: BBox


@dataclass(frozen=True)
class TrackState:
    """Tier 2 output: a tracked object with its geometry facts up to `at`."""

    track_id: int
    label: str
    bbox: BBox
    at: datetime
    velocity: tuple[float, float]  # normalized units / second
    dwell_s: float
    zones: tuple[str, ...]  # zones currently containing the track's anchor point
    approach: float | None  # velocity component toward the nearest entry/object zone
    loiter: bool
    repeat_pass: int
    touch: bool


@dataclass(frozen=True)
class SceneVerdict:
    """Tier 3 output: a structured judgment — never free prose parsed by regex."""

    activity: str
    intent_label: str
    intent_score: float
    summary: str
    model: str


@dataclass(frozen=True)
class CascadeBudget:
    """Hard budgets; exceeding them degrades tiers in shedding-ladder order, never stalls."""

    detect_fps: float = 5.0
    vlm_calls_per_minute: int = 6
    max_queue: int = 64


class MotionGate(Protocol):
    """Tier 0: substream frames in, motion regions out. Must be ~free.

    Timestamps are epoch-second floats end to end (matching the DB boundary rule).
    """

    def process(self, ts: float, image: object) -> list[MotionRegion]: ...


class Detector(Protocol):
    """Tier 1: inference on one frame (ONNX Runtime, permissive models only).

    Single-frame by measurement: at cascade rates (≤5 fps per camera, motion-gated) the
    batching win is negligible while the latency and queueing complexity are not. Cross-
    camera batching can return behind this same protocol if profiling ever justifies it.
    """

    async def infer(self, image: object) -> list[Detection]: ...


class Tracker(Protocol):
    """Tier 2: detections in, tracks with geometry facts out (pure math + zone algebra)."""

    def update(self, ts: float, detections: Sequence[Detection]) -> list[TrackState]: ...


class Understander(Protocol):
    """Tier 3: selected keyframes + a structured question in, a SceneVerdict out."""

    async def judge(self, frames: Sequence[object], question: str) -> SceneVerdict: ...


@dataclass
class CascadeSpec:
    """What the M2 orchestrator will wire; kept as an executable statement of intent."""

    budget: CascadeBudget = field(default_factory=CascadeBudget)
    # Promotion rules (T2 → T3), mirroring docs/architecture/ai-pipeline.md:
    promote_on_touch: bool = True
    promote_on_entry_zone: bool = True
    promote_dwell_s: float = 10.0
    promote_on_loiter: bool = True
    promote_on_repeat_pass: int = 3
