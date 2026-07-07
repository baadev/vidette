from __future__ import annotations

from datetime import UTC, datetime, timedelta

from vidette.core.config import Retention
from vidette.recording.retention import Segment, SegmentClass, plan_deletions

NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


def seg(klass: SegmentClass, age_days: float, size: int = 100) -> Segment:
    end = NOW - timedelta(days=age_days)
    return Segment(
        camera="cam",
        start=end - timedelta(seconds=10),
        end=end,
        path=f"/media/{klass.value}-{age_days}.mp4",
        size_bytes=size,
        klass=klass,
    )


RETENTION = Retention()  # defaults: continuous 3d, motion 14d, events 90d, favorites forever


def test_expiry_respects_class_schedules() -> None:
    segments = [
        seg(SegmentClass.continuous, age_days=4),   # expired (3d)
        seg(SegmentClass.continuous, age_days=1),   # kept
        seg(SegmentClass.motion, age_days=15),      # expired (14d)
        seg(SegmentClass.event, age_days=15),       # kept — events live 90d
        seg(SegmentClass.event, age_days=91),       # expired
        seg(SegmentClass.favorite, age_days=1000),  # immortal
    ]
    plan = plan_deletions(segments, RETENTION, now=NOW)
    expired_paths = {s.path for s in plan.expired}
    assert expired_paths == {
        "/media/continuous-4.mp4",
        "/media/motion-15.mp4",
        "/media/event-91.mp4",
    }
    assert plan.pressure == []


def test_pressure_deletes_oldest_continuous_then_motion_never_events() -> None:
    segments = [
        seg(SegmentClass.continuous, age_days=2, size=100),
        seg(SegmentClass.continuous, age_days=1, size=100),
        seg(SegmentClass.motion, age_days=5, size=100),
        seg(SegmentClass.event, age_days=5, size=100),
    ]
    plan = plan_deletions(segments, RETENTION, now=NOW, bytes_to_free=250)
    assert [s.path for s in plan.pressure] == [
        "/media/continuous-2.mp4",  # oldest continuous first
        "/media/continuous-1.mp4",
        "/media/motion-5.mp4",      # then motion
    ]
    assert plan.unmet_bytes == 0
    assert plan.freed_bytes == 300


def test_pressure_never_touches_events_and_reports_unmet() -> None:
    segments = [
        seg(SegmentClass.event, age_days=5, size=100),
        seg(SegmentClass.favorite, age_days=5, size=100),
    ]
    plan = plan_deletions(segments, RETENTION, now=NOW, bytes_to_free=500)
    assert plan.pressure == []
    assert plan.unmet_bytes == 500  # caller must raise a loud storage.pressure event


def test_expired_bytes_count_toward_pressure_target() -> None:
    segments = [
        seg(SegmentClass.continuous, age_days=4, size=400),  # expired anyway
        seg(SegmentClass.continuous, age_days=1, size=100),
    ]
    plan = plan_deletions(segments, RETENTION, now=NOW, bytes_to_free=300)
    assert plan.expired and not plan.pressure  # expiry alone satisfied the pressure target
