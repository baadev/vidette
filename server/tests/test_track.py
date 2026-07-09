"""Tier-2 tracker tests: pure geometry helpers + scripted detection sequences.

Scripted scenes use a fixed zone map (normalized 0..1, y grows downward):

    sidewalk  public   full-width band y 0.85..1.0
    porch     private  box x 0.2..0.8, y 0.3..0.7
    door      entry    box x 0.4..0.6, y 0.1..0.3

Detections are built from the *anchor* (bbox bottom-center — feet) with boxes large
enough that scripted steps keep IoU above the match threshold.
"""

from __future__ import annotations

from datetime import UTC

import pytest

from vidette.core.config import Zone, ZoneKind
from vidette.pipeline.base import BBox, Detection
from vidette.pipeline.track import (
    IouTracker,
    anchor_point,
    approach_score,
    greedy_iou_match,
    iou,
    is_loitering,
    point_in_polygon,
    polygon_centroid,
    zones_containing,
)

SQUARE = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
DIAMOND = [(0.5, 0.0), (1.0, 0.5), (0.5, 1.0), (0.0, 0.5)]
# A "U" shape: the notch between the prongs is outside the polygon.
U_SHAPE = [
    (0.0, 0.0),
    (0.9, 0.0),
    (0.9, 0.9),
    (0.6, 0.9),
    (0.6, 0.3),
    (0.3, 0.3),
    (0.3, 0.9),
    (0.0, 0.9),
]


def make_zones() -> dict[str, Zone]:
    return {
        "sidewalk": Zone(
            kind=ZoneKind.public,
            points=[(0.0, 0.85), (1.0, 0.85), (1.0, 1.0), (0.0, 1.0)],
        ),
        "porch": Zone(
            kind=ZoneKind.private,
            points=[(0.2, 0.3), (0.8, 0.3), (0.8, 0.7), (0.2, 0.7)],
        ),
        "door": Zone(
            kind=ZoneKind.entry,
            points=[(0.4, 0.1), (0.6, 0.1), (0.6, 0.3), (0.4, 0.3)],
        ),
    }


def person(ax: float, ay: float, *, conf: float = 0.9, w: float = 0.2, h: float = 0.4) -> Detection:
    """A detection whose anchor (bbox bottom-center) lands exactly at (ax, ay)."""
    return Detection(label="person", confidence=conf, bbox=BBox(x=ax - w / 2, y=ay - h, w=w, h=h))


# --- point_in_polygon ---------------------------------------------------------------------------


def test_point_in_polygon_square() -> None:
    assert point_in_polygon((0.5, 0.5), SQUARE)
    assert not point_in_polygon((1.5, 0.5), SQUARE)
    assert not point_in_polygon((0.5, -0.1), SQUARE)


def test_point_in_polygon_diamond_excludes_bbox_corners() -> None:
    # Inside the diamond's bounding box but outside the diamond itself.
    assert point_in_polygon((0.5, 0.5), DIAMOND)
    assert not point_in_polygon((0.1, 0.1), DIAMOND)
    assert not point_in_polygon((0.9, 0.9), DIAMOND)


def test_point_in_polygon_ray_through_vertex_counts_once() -> None:
    # The +x ray from this point passes exactly through the vertex at (1.0, 0.5); the
    # half-open edge rule must count one crossing, not two.
    assert point_in_polygon((0.25, 0.5), DIAMOND)


def test_point_in_polygon_concave_notch() -> None:
    assert not point_in_polygon((0.45, 0.6), U_SHAPE)  # in the notch between the prongs
    assert point_in_polygon((0.15, 0.6), U_SHAPE)  # left prong
    assert point_in_polygon((0.75, 0.6), U_SHAPE)  # right prong
    assert point_in_polygon((0.45, 0.15), U_SHAPE)  # the bridge above the notch


def test_zones_containing_reports_all_hits_in_config_order() -> None:
    zones = make_zones()
    assert zones_containing((0.5, 0.9), zones) == ("sidewalk",)
    assert zones_containing((0.5, 0.5), zones) == ("porch",)
    assert zones_containing((0.5, 0.2), zones) == ("door",)
    assert zones_containing((0.05, 0.5), zones) == ()


# --- iou / anchors / centroids ------------------------------------------------------------------


def test_iou_identical_and_disjoint() -> None:
    box = BBox(0.1, 0.1, 0.2, 0.2)
    assert iou(box, box) == pytest.approx(1.0)
    assert iou(box, BBox(0.5, 0.5, 0.2, 0.2)) == 0.0
    # Touching edges do not overlap (exactly representable coords: 0.25 + 0.25 == 0.5).
    assert iou(BBox(0.25, 0.25, 0.25, 0.25), BBox(0.5, 0.25, 0.25, 0.25)) == 0.0


def test_iou_half_overlap() -> None:
    a = BBox(0.0, 0.0, 0.2, 0.2)
    b = BBox(0.1, 0.0, 0.2, 0.2)  # half of each box overlaps
    assert iou(a, b) == pytest.approx(1.0 / 3.0)


def test_anchor_is_bottom_center() -> None:
    assert anchor_point(BBox(0.4, 0.1, 0.2, 0.3)) == pytest.approx((0.5, 0.4))


def test_polygon_centroid_square() -> None:
    assert polygon_centroid(SQUARE) == pytest.approx((0.5, 0.5))


def test_greedy_iou_match_prefers_best_pair() -> None:
    tracks = [BBox(0.0, 0.0, 0.2, 0.2), BBox(0.5, 0.5, 0.2, 0.2)]
    detections = [BBox(0.52, 0.5, 0.2, 0.2), BBox(0.02, 0.0, 0.2, 0.2)]
    assert sorted(greedy_iou_match(tracks, detections, 0.3)) == [(0, 1), (1, 0)]


def test_greedy_iou_match_respects_threshold() -> None:
    assert greedy_iou_match([BBox(0.0, 0.0, 0.1, 0.1)], [BBox(0.5, 0.5, 0.1, 0.1)], 0.3) == []


# --- approach -----------------------------------------------------------------------------------


def test_approach_none_without_targets() -> None:
    assert approach_score((0.0, -0.5), (0.5, 0.8), []) is None


def test_approach_is_cosine_weighted_speed() -> None:
    target = (0.5, 0.2)
    # Moving straight at the target: full speed; walking past: ~0; walking away: clamped 0.
    assert approach_score((0.0, -0.1), (0.5, 0.8), [target]) == pytest.approx(0.1)
    assert approach_score((0.1, 0.0), (0.5, 0.8), [target]) == pytest.approx(0.0)
    assert approach_score((0.0, 0.1), (0.5, 0.8), [target]) == 0.0


def test_approach_clamped_to_one_and_zero_at_target() -> None:
    assert approach_score((0.0, -5.0), (0.5, 0.8), [(0.5, 0.2)]) == 1.0
    assert approach_score((0.0, -0.5), (0.5, 0.2), [(0.5, 0.2)]) == 0.0


def test_approach_uses_nearest_target() -> None:
    near, far = (0.5, 0.2), (5.0, 5.0)
    # Velocity points at `near` and away from `far`: the nearest target must win.
    assert approach_score((0.0, -0.1), (0.5, 0.8), [far, near]) == pytest.approx(0.1)


# --- loiter -------------------------------------------------------------------------------------


def test_loiter_requires_two_points_and_min_path() -> None:
    assert not is_loitering([])
    assert not is_loitering([(0.5, 0.5)])
    # Sub-jitter wiggle: path below min_path never loiters, however extreme the ratio.
    assert not is_loitering([(0.5, 0.5), (0.501, 0.5), (0.5, 0.5), (0.501, 0.5)])


def test_loiter_pacing_true_straight_walk_false() -> None:
    pacing = [(0.4 if i % 2 == 0 else 0.5, 0.5) for i in range(11)]
    assert is_loitering(pacing)
    straight = [(0.1 + 0.05 * i, 0.5) for i in range(11)]
    assert not is_loitering(straight)


# --- tracker: birth / match / death -------------------------------------------------------------


def test_birth_from_high_conf_detection() -> None:
    tracker = IouTracker(make_zones())
    states = tracker.update(100.0, [person(0.5, 0.9)])
    assert len(states) == 1
    state = states[0]
    assert state.track_id == 1
    assert state.label == "person"
    assert state.velocity == (0.0, 0.0)
    assert state.at.tzinfo is UTC
    assert state.at.timestamp() == pytest.approx(100.0)


def test_low_conf_detection_never_births() -> None:
    tracker = IouTracker(make_zones())
    assert tracker.update(0.0, [person(0.5, 0.9, conf=0.3)]) == []
    assert tracker.update(1.0, [person(0.5, 0.9, conf=0.24)]) == []  # below low_conf: ignored


def test_match_keeps_id_and_updates_velocity_ema() -> None:
    tracker = IouTracker(make_zones())
    tracker.update(0.0, [person(0.5, 0.8)])
    states = tracker.update(1.0, [person(0.5, 0.7)])
    assert states[0].track_id == 1
    # raw velocity (0, -0.1) blended with the newborn's (0, 0) at alpha 0.5:
    assert states[0].velocity == pytest.approx((0.0, -0.05))


def test_low_conf_keeps_track_alive_across_flaky_frames() -> None:
    tracker = IouTracker(make_zones())
    tracker.update(0.0, [person(0.5, 0.9)])
    for ts in (1.0, 2.0, 3.0, 4.0):
        states = tracker.update(ts, [person(0.5, 0.9, conf=0.3)])
        assert [s.track_id for s in states] == [1]
    # 4 s since the last high-confidence hit — alive only thanks to stage 2.
    states = tracker.update(5.0, [person(0.5, 0.9)])
    assert [s.track_id for s in states] == [1]


def test_track_dies_after_max_age_and_id_is_not_reused() -> None:
    tracker = IouTracker(make_zones(), max_age_s=3.0)
    tracker.update(0.0, [person(0.5, 0.9)])
    assert [s.track_id for s in tracker.update(2.0, [])] == [1]  # coasting, still alive
    states = tracker.update(4.0, [person(0.5, 0.9)])
    assert [s.track_id for s in states] == [2]  # the old track died; fresh identity


def test_two_tracks_match_their_nearest_detections() -> None:
    tracker = IouTracker(make_zones())
    tracker.update(0.0, [person(0.3, 0.9), person(0.7, 0.9)])
    states = tracker.update(1.0, [person(0.72, 0.9), person(0.32, 0.9)])
    by_id = {state.track_id: state for state in states}
    assert anchor_point(by_id[1].bbox) == pytest.approx((0.32, 0.9))
    assert anchor_point(by_id[2].bbox) == pytest.approx((0.72, 0.9))


# --- tracker: zone features ---------------------------------------------------------------------


def test_dwell_accumulates_from_first_nonpublic_entry() -> None:
    tracker = IouTracker(make_zones())
    assert tracker.update(0.0, [person(0.5, 0.9)])[0].dwell_s == 0.0  # public only
    assert tracker.update(1.0, [person(0.5, 0.75)])[0].dwell_s == 0.0  # no zone at all
    assert tracker.update(2.0, [person(0.5, 0.6)])[0].dwell_s == 0.0  # porch: clock starts
    assert tracker.update(5.0, [person(0.5, 0.55)])[0].dwell_s == pytest.approx(3.0)
    assert tracker.update(7.0, [person(0.5, 0.6)])[0].dwell_s == pytest.approx(5.0)


def test_dwell_zero_while_only_public() -> None:
    tracker = IouTracker(make_zones())
    for ts in range(6):
        state = tracker.update(float(ts), [person(0.3 + 0.05 * ts, 0.9)])[0]
        assert state.dwell_s == 0.0
        assert state.zones == ("sidewalk",)


def test_approach_toward_entry_zone_moving_vs_static() -> None:
    tracker = IouTracker(make_zones())
    tracker.update(0.0, [person(0.5, 0.8)])
    moving = tracker.update(1.0, [person(0.5, 0.7)])[0]
    assert moving.approach is not None and moving.approach == pytest.approx(0.05)

    static_tracker = IouTracker(make_zones())
    static_tracker.update(0.0, [person(0.5, 0.8)])
    static = static_tracker.update(1.0, [person(0.5, 0.8)])[0]
    assert static.approach == pytest.approx(0.0)


def test_approach_none_when_no_entry_or_object_zones() -> None:
    zones = {name: zone for name, zone in make_zones().items() if name != "door"}
    tracker = IouTracker(zones)
    tracker.update(0.0, [person(0.5, 0.8)])
    assert tracker.update(1.0, [person(0.5, 0.7)])[0].approach is None


def test_touch_requires_slow_hold_inside_entry_zone() -> None:
    tracker = IouTracker(make_zones())
    assert tracker.update(0.0, [person(0.5, 0.25)])[0].touch is False  # hold starts
    assert tracker.update(1.0, [person(0.5, 0.25)])[0].touch is False  # 1 s < hold
    state = tracker.update(2.0, [person(0.5, 0.25)])[0]
    assert state.touch is True  # 2 s of near-zero speed at the door
    assert state.zones == ("door",)


def test_touch_false_when_moving_inside_entry_zone() -> None:
    tracker = IouTracker(make_zones())
    x = 0.45
    for ts in range(5):
        state = tracker.update(float(ts), [person(x, 0.25)])[0]
        assert state.touch is False
        x += 0.05  # ~0.05 units/s — walking through, not touching


def test_touch_false_when_slow_but_not_in_entry_zone() -> None:
    tracker = IouTracker(make_zones())
    for ts in range(5):
        assert tracker.update(float(ts), [person(0.5, 0.5)])[0].touch is False  # porch


def test_loiter_pacing_in_private_zone() -> None:
    tracker = IouTracker(make_zones())
    saw_loiter = False
    for ts in range(13):
        x = 0.45 if ts % 2 == 0 else 0.55
        state = tracker.update(float(ts), [person(x, 0.5)])[0]
        saw_loiter = saw_loiter or state.loiter
    assert saw_loiter


def test_no_loiter_when_pacing_in_public_zone() -> None:
    tracker = IouTracker(make_zones())
    for ts in range(13):
        x = 0.45 if ts % 2 == 0 else 0.55
        assert tracker.update(float(ts), [person(x, 0.9)])[0].loiter is False


def test_no_loiter_on_straight_walk_through_private_zone() -> None:
    tracker = IouTracker(make_zones())
    for ts in range(10):
        state = tracker.update(float(ts), [person(0.25 + 0.05 * ts, 0.5)])[0]
        assert state.loiter is False


def test_repeat_pass_counts_reentries() -> None:
    tracker = IouTracker(make_zones())
    # Bounce between sidewalk (public, y=0.87) and porch (private, y=0.68) at 1 Hz — steps
    # of 0.19 keep IoU above the match threshold for the 0.2×0.4 test boxes. The first
    # entry is not a re-entry; each later entry bumps the counter.
    script = [
        (0.87, 0),  # sidewalk
        (0.68, 0),  # porch: first entry
        (0.87, 0),  # back out
        (0.68, 1),  # first re-entry
        (0.87, 1),
        (0.68, 2),  # second re-entry
    ]
    for ts, (ay, expected) in enumerate(script):
        state = tracker.update(float(ts), [person(0.5, ay)])[0]
        assert state.repeat_pass == expected, f"at ts={ts}"


def test_repeat_pass_zero_for_continuous_presence() -> None:
    tracker = IouTracker(make_zones())
    for ts in range(8):
        assert tracker.update(float(ts), [person(0.5, 0.5)])[0].repeat_pass == 0


# --- the passers-by fields ----------------------------------------------------------------------


def test_public_only_walker_shows_no_promotable_facts() -> None:
    """A track that never leaves the sidewalk carries nothing the engine could promote —
    the field-level half of the passers-by suppression rule."""
    tracker = IouTracker(make_zones())
    for ts in range(12):
        state = tracker.update(float(ts), [person(0.15 + 0.05 * ts, 0.92)])[0]
        assert state.zones == ("sidewalk",)
        assert state.dwell_s == 0.0
        assert state.touch is False
        assert state.loiter is False
        assert state.repeat_pass == 0
