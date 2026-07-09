"""Tier 2 — trajectory geometry: two-stage greedy IoU tracking + zone algebra.

Association is ByteTrack-*style* (docs/architecture/ai-pipeline.md): a first pass matches
high-confidence detections to live tracks by IoU, a second pass lets low-confidence
detections keep existing tracks alive (they never birth new tracks). There is no Kalman
filter — ByteTrack proper (motion-predicted boxes) is a planned upgrade; at substream
detection rates greedy IoU over raw boxes is already stable, and the features below carry
most of the intent signal either way.

Everything past association is pure math over normalized (0..1) coordinates. Each rule is
a small pure helper with unit tests (tests/test_track.py):

- ``point_in_polygon``  — ray casting, no dependencies
- ``approach_score``    — cosine-weighted speed toward the nearest entry/object centroid
- ``is_loitering``      — path length / displacement ratio over a rolling window
- dwell / touch / repeat_pass — bookkeeping in `IouTracker`, thresholds are module constants
"""

from __future__ import annotations

import itertools
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from vidette.core.config import Zone, ZoneKind
from vidette.pipeline.base import BBox, Detection, TrackState

Point = tuple[float, float]

# Feature thresholds (normalized units — the frame is 1×1; speeds are units/second).
TOUCH_MAX_SPEED = 0.02  # "near-zero velocity" for the touch rule
TOUCH_HOLD_S = 2.0  # how long the near-zero dwell inside an entry/object zone must last
LOITER_WINDOW_S = 10.0  # kinematics window for the loiter ratio
LOITER_RATIO = 4.0  # path length / displacement above this ⇒ pacing, waiting
LOITER_MIN_PATH = 0.05  # sub-jitter paths never count as loitering (a static track is calm)
REPEAT_WINDOW_S = 120.0  # re-entries into non-public zones are counted within this window
VELOCITY_EMA_ALPHA = 0.5  # weight of the newest anchor delta in the velocity EMA

_EPS = 1e-6


# --- pure geometry helpers ---------------------------------------------------------------------


def point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
    """Even-odd ray-casting containment test.

    Points exactly on an edge follow the usual half-open convention: results are
    deterministic but not guaranteed inside/outside — zones should overlap the area they
    mean to cover, not kiss it.
    """
    x, y = point
    inside = False
    count = len(polygon)
    j = count - 1
    for i in range(count):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y):
            x_cross = xi + (y - yi) * (xj - xi) / (yj - yi)
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def iou(a: BBox, b: BBox) -> float:
    """Intersection-over-union of two normalized boxes; 0.0 when disjoint or degenerate."""
    left = max(a.x, b.x)
    top = max(a.y, b.y)
    right = min(a.x + a.w, b.x + b.w)
    bottom = min(a.y + a.h, b.y + b.h)
    if right <= left or bottom <= top:
        return 0.0
    intersection = (right - left) * (bottom - top)
    union = a.w * a.h + b.w * b.h - intersection
    if union <= _EPS:
        return 0.0
    return intersection / union


def anchor_point(bbox: BBox) -> Point:
    """The track's ground contact: bottom-center of the box — feet, wheels, paws."""
    return (bbox.x + bbox.w / 2.0, bbox.y + bbox.h)


def polygon_centroid(points: Sequence[Point]) -> Point:
    """Vertex mean — good enough as an aiming point for the approach feature."""
    count = len(points)
    return (sum(x for x, _ in points) / count, sum(y for _, y in points) / count)


def zones_containing(point: Point, zones: Mapping[str, Zone]) -> tuple[str, ...]:
    """Names of the zones whose polygon contains `point`, in configuration order."""
    return tuple(name for name, zone in zones.items() if point_in_polygon(point, zone.points))


def approach_score(velocity: Point, anchor: Point, targets: Sequence[Point]) -> float | None:
    """Cosine-weighted speed toward the nearest target centroid, clamped to 0..1.

    The dot product of the velocity with the unit direction to the target *is*
    ``speed × cos(angle)`` — moving straight at the door scores the full speed, walking
    past scores ~0, walking away clamps to 0. None when no targets are configured.
    """
    if not targets:
        return None
    nearest = min(targets, key=lambda t: math.hypot(t[0] - anchor[0], t[1] - anchor[1]))
    dx = nearest[0] - anchor[0]
    dy = nearest[1] - anchor[1]
    distance = math.hypot(dx, dy)
    if distance < _EPS:
        return 0.0  # already there — "approach" is over
    score = (velocity[0] * dx + velocity[1] * dy) / distance
    return min(max(score, 0.0), 1.0)


def is_loitering(
    points: Sequence[Point],
    *,
    ratio: float = LOITER_RATIO,
    min_path: float = LOITER_MIN_PATH,
) -> bool:
    """Pacing detector: path length / displacement over the window exceeds `ratio`.

    A straight walk scores ~1, pacing back and forth scores high; `min_path` keeps a
    static (or jittering) track from dividing noise by ~zero and crying wolf.
    """
    if len(points) < 2:
        return False
    path = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in itertools.pairwise(points))
    if path < min_path:
        return False
    first, last = points[0], points[-1]
    displacement = math.hypot(last[0] - first[0], last[1] - first[1])
    return path / max(displacement, _EPS) > ratio


def greedy_iou_match(
    track_boxes: Sequence[BBox], detection_boxes: Sequence[BBox], iou_threshold: float
) -> list[tuple[int, int]]:
    """Greedy best-first assignment: (track_index, detection_index) pairs, highest IoU first.

    Each track and each detection is used at most once; pairs below the threshold never
    match. O(n·m log(n·m)) — fine for the handful of concurrent tracks a camera sees.
    """
    scored: list[tuple[float, int, int]] = []
    for track_index, track_box in enumerate(track_boxes):
        for det_index, det_box in enumerate(detection_boxes):
            overlap = iou(track_box, det_box)
            if overlap >= iou_threshold:
                scored.append((overlap, track_index, det_index))
    scored.sort(key=lambda item: item[0], reverse=True)
    matched: list[tuple[int, int]] = []
    used_tracks: set[int] = set()
    used_detections: set[int] = set()
    for _, track_index, det_index in scored:
        if track_index in used_tracks or det_index in used_detections:
            continue
        used_tracks.add(track_index)
        used_detections.add(det_index)
        matched.append((track_index, det_index))
    return matched


# --- the tracker --------------------------------------------------------------------------------


@dataclass
class _Track:
    """Mutable per-track state; the public face is the frozen TrackState snapshot."""

    track_id: int
    label: str
    bbox: BBox
    anchor: Point
    last_seen_ts: float
    velocity: Point = (0.0, 0.0)
    history: list[tuple[float, Point]] = field(default_factory=list)  # (ts, anchor)
    first_nonpublic_ts: float | None = None
    in_nonpublic: bool = False
    entered_nonpublic_once: bool = False
    reentry_ts: list[float] = field(default_factory=list)
    slow_in_target_since: float | None = None


class IouTracker:
    """Two-stage greedy IoU tracker (ByteTrack-style association; ByteTrack proper — with
    Kalman-predicted boxes — is a planned upgrade) plus the Tier-2 zone/kinematics algebra.

    `update(ts, detections)` is the whole interface: epoch-second timestamps in, a
    `TrackState` per live track out. Unseen tracks coast (last box, decaying relevance)
    until `max_age_s`, then die; ids are stable ints and never reused within a tracker.
    """

    def __init__(
        self,
        zones: dict[str, Zone],
        *,
        high_conf: float = 0.5,
        low_conf: float = 0.25,
        iou_match: float = 0.3,
        max_age_s: float = 3.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._zones = dict(zones)
        self._zone_kinds = {name: zone.kind for name, zone in self._zones.items()}
        self._targets = [
            polygon_centroid(zone.points)
            for zone in self._zones.values()
            if zone.kind in (ZoneKind.entry, ZoneKind.object)
        ]
        self._high_conf = high_conf
        self._low_conf = low_conf
        self._iou_match = iou_match
        self._max_age_s = max_age_s
        self._clock = clock or time.time
        self._tracks: dict[int, _Track] = {}
        self._next_id = 1

    def live_track_ids(self) -> frozenset[int]:
        return frozenset(self._tracks)

    def update(self, ts: float, detections: list[Detection]) -> list[TrackState]:
        # Age out tracks unseen for longer than max_age_s *before* matching — a stale box
        # must not steal a fresh detection from a newborn.
        self._tracks = {
            track_id: track
            for track_id, track in self._tracks.items()
            if ts - track.last_seen_ts <= self._max_age_s
        }

        high = [det for det in detections if det.confidence >= self._high_conf]
        low = [det for det in detections if self._low_conf <= det.confidence < self._high_conf]

        tracks = list(self._tracks.values())
        unmatched_tracks = list(range(len(tracks)))

        # Stage 1: high-confidence detections vs. all live tracks.
        stage1 = greedy_iou_match(
            [track.bbox for track in tracks], [det.bbox for det in high], self._iou_match
        )
        matched_high: set[int] = set()
        for track_index, det_index in stage1:
            self._apply_match(tracks[track_index], high[det_index], ts)
            unmatched_tracks.remove(track_index)
            matched_high.add(det_index)

        # Stage 2: the tracks nobody claimed vs. low-confidence detections — enough to keep
        # a track alive through a flaky frame, never enough to create one.
        remaining = [tracks[index] for index in unmatched_tracks]
        stage2 = greedy_iou_match(
            [track.bbox for track in remaining], [det.bbox for det in low], self._iou_match
        )
        for track_index, det_index in stage2:
            self._apply_match(remaining[track_index], low[det_index], ts)

        # Births: unmatched high-confidence detections become new tracks.
        for det_index, det in enumerate(high):
            if det_index in matched_high:
                continue
            anchor = anchor_point(det.bbox)
            track = _Track(
                track_id=self._next_id,
                label=det.label,
                bbox=det.bbox,
                anchor=anchor,
                last_seen_ts=ts,
                history=[(ts, anchor)],
            )
            self._next_id += 1
            self._tracks[track.track_id] = track

        return [
            self._state_for(track, ts)
            for track in sorted(self._tracks.values(), key=lambda t: t.track_id)
        ]

    # --- per-track bookkeeping -------------------------------------------------------------

    def _apply_match(self, track: _Track, det: Detection, ts: float) -> None:
        dt = ts - track.last_seen_ts
        new_anchor = anchor_point(det.bbox)
        if dt > _EPS:
            raw = (
                (new_anchor[0] - track.anchor[0]) / dt,
                (new_anchor[1] - track.anchor[1]) / dt,
            )
            alpha = VELOCITY_EMA_ALPHA
            track.velocity = (
                alpha * raw[0] + (1.0 - alpha) * track.velocity[0],
                alpha * raw[1] + (1.0 - alpha) * track.velocity[1],
            )
        track.bbox = det.bbox
        track.anchor = new_anchor
        track.last_seen_ts = ts
        track.history.append((ts, new_anchor))

    def _state_for(self, track: _Track, ts: float) -> TrackState:
        track.history = [(t, p) for t, p in track.history if ts - t <= LOITER_WINDOW_S]

        zones = zones_containing(track.anchor, self._zones)
        kinds = {self._zone_kinds[name] for name in zones}
        in_nonpublic = any(kind is not ZoneKind.public for kind in kinds)

        # dwell: seconds since the track *first* entered any non-public zone; reads 0.0
        # while the track is only in public zones (or none) — deliberately simple at M2.
        if in_nonpublic and track.first_nonpublic_ts is None:
            track.first_nonpublic_ts = ts
        dwell_s = (
            ts - track.first_nonpublic_ts
            if in_nonpublic and track.first_nonpublic_ts is not None
            else 0.0
        )

        # repeat_pass: distinct re-entries (entries after the first) into non-public zones
        # within the rolling window — the "casing the place" counter.
        if in_nonpublic and not track.in_nonpublic:
            if track.entered_nonpublic_once:
                track.reentry_ts.append(ts)
            track.entered_nonpublic_once = True
        track.in_nonpublic = in_nonpublic
        track.reentry_ts = [t for t in track.reentry_ts if ts - t <= REPEAT_WINDOW_S]
        repeat_pass = len(track.reentry_ts)

        approach = approach_score(track.velocity, track.anchor, self._targets)

        # touch: near-zero speed while the anchor sits inside an entry/object zone,
        # sustained for TOUCH_HOLD_S — hands on the door handle, not a walk-past.
        in_target = any(kind in (ZoneKind.entry, ZoneKind.object) for kind in kinds)
        speed = math.hypot(track.velocity[0], track.velocity[1])
        if in_target and speed < TOUCH_MAX_SPEED:
            if track.slow_in_target_since is None:
                track.slow_in_target_since = ts
            touch = ts - track.slow_in_target_since >= TOUCH_HOLD_S
        else:
            track.slow_in_target_since = None
            touch = False

        loiter = in_nonpublic and is_loitering([point for _, point in track.history])

        return TrackState(
            track_id=track.track_id,
            label=track.label,
            bbox=track.bbox,
            at=datetime.fromtimestamp(ts, tz=UTC),
            velocity=track.velocity,
            dwell_s=dwell_s,
            zones=zones,
            approach=approach,
            loiter=loiter,
            repeat_pass=repeat_pass,
            touch=touch,
        )
