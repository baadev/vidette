"""Event engine tests: scripted detections through a real IouTracker + real bus.

The engine under test gets a real `IouTracker` (built from the configured zones), a real
`InProcessEventBus`, an in-memory `FakeEventDb` conforming to the Database event methods,
and a fake `snapshot_fn`. The real `Database` event methods + the V2 migration get their
own round-trip test at the bottom.

Zone map (same shape as tests/test_track.py): sidewalk public y 0.85..1, porch private
x 0.2..0.8 / y 0.3..0.7, door entry x 0.4..0.6 / y 0.1..0.3.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from vidette.core.config import Sensitivity, VidetteConfig, ZoneKind
from vidette.core.events import InProcessEventBus, Subscription
from vidette.db import Database, SegmentRow
from vidette.events.engine import (
    SNAPSHOT_MAX_ATTEMPTS,
    EventEngine,
    is_suppressed,
    promotion_reason,
)
from vidette.pipeline.base import BBox, CascadeSpec, Detection, TrackState

T0 = 1_751_900_000.0  # arbitrary fixed epoch


def make_config(
    tmp_path: Path, media_dir: Path, policies: list[dict[str, Any]] | None = None
) -> VidetteConfig:
    return VidetteConfig.model_validate(
        {
            "storage": {
                "media_dir": str(media_dir),
                "database": str(tmp_path / "vidette.db"),
            },
            "cameras": {
                "front-door": {
                    "adapter": "rtsp",
                    "source": {"main": "rtsp://user:pw@203.0.113.10:554/stream1"},
                    "zones": {
                        "sidewalk": {
                            "kind": "public",
                            "points": [[0.0, 0.85], [1.0, 0.85], [1.0, 1.0], [0.0, 1.0]],
                        },
                        "porch": {
                            "kind": "private",
                            "points": [[0.2, 0.3], [0.8, 0.3], [0.8, 0.7], [0.2, 0.7]],
                        },
                        "door": {
                            "kind": "entry",
                            "points": [[0.4, 0.1], [0.6, 0.1], [0.6, 0.3], [0.4, 0.3]],
                        },
                    },
                }
            },
            "policies": policies or [],
        }
    )


class FakeEventDb:
    """In-memory stand-in conforming to the Database event methods the engine uses."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.segments: list[SegmentRow] = []
        self.upgrade_calls: list[tuple[str, float, float, str, tuple[str, ...]]] = []
        self.insert_error: Exception | None = None

    async def insert_event(
        self,
        event_id: str,
        camera: str,
        started_at: float,
        state: str,
        kinds: list[str],
        zones: list[str],
        geometry: dict[str, Any],
        *,
        ended_at: float | None = None,
        summary: str | None = None,
        intent: dict[str, Any] | None = None,
        policy: str | None = None,
        snapshot_path: str | None = None,
        clip_path: str | None = None,
    ) -> None:
        if self.insert_error is not None:
            raise self.insert_error
        self.rows[event_id] = {
            "id": event_id,
            "camera": camera,
            "started_at": started_at,
            "ended_at": ended_at,
            "state": state,
            "kinds": kinds,
            "zones": zones,
            "geometry": geometry,
            "summary": summary,
            "intent": intent,
            "policy": policy,
            "feedback": None,
            "snapshot_path": snapshot_path,
            "clip_path": clip_path,
        }

    async def update_event(
        self,
        event_id: str,
        *,
        state: str | None = None,
        ended_at: float | None = None,
        snapshot_path: str | None = None,
        clip_path: str | None = None,
    ) -> None:
        row = self.rows[event_id]
        for column, value in (
            ("state", state),
            ("ended_at", ended_at),
            ("snapshot_path", snapshot_path),
            ("clip_path", clip_path),
        ):
            if value is not None:
                row[column] = value

    async def upgrade_segments_class(
        self,
        camera: str,
        start_ts: float,
        end_ts: float,
        new_klass: str,
        *,
        only_from: Sequence[str] = ("continuous", "motion"),
    ) -> int:
        self.upgrade_calls.append((camera, start_ts, end_ts, new_klass, tuple(only_from)))
        upgraded = 0
        for index, segment in enumerate(self.segments):
            if (
                segment.camera == camera
                and segment.end_ts > start_ts
                and segment.start_ts < end_ts
                and segment.klass in only_from
            ):
                self.segments[index] = replace(segment, klass=new_klass)
                upgraded += 1
        return upgraded


def seg(seg_id: int, start_ts: float, end_ts: float, klass: str = "continuous") -> SegmentRow:
    return SegmentRow(
        id=seg_id,
        camera="front-door",
        start_ts=start_ts,
        end_ts=end_ts,
        path=f"/media/front-door/{seg_id}.mp4",
        size_bytes=1,
        klass=klass,
        codec="h264",
    )


def person(ax: float, ay: float, *, conf: float = 0.9) -> Detection:
    w, h = 0.2, 0.4
    return Detection(label="person", confidence=conf, bbox=BBox(x=ax - w / 2, y=ay - h, w=w, h=h))


async def snapshot_ok(camera: str) -> bytes:
    return b"\xff\xd8\xff\xe0-fake-jpeg-" + camera.encode()


async def snapshot_boom(camera: str) -> bytes:
    raise RuntimeError(f"gateway has no stream '{camera}'")


def make_engine(
    tmp_path: Path,
    media_dir: Path,
    *,
    policies: list[dict[str, Any]] | None = None,
    snapshot_fn: Any = snapshot_ok,
) -> tuple[EventEngine, FakeEventDb, InProcessEventBus, Subscription]:
    config = make_config(tmp_path, media_dir, policies)
    db = FakeEventDb()
    bus = InProcessEventBus()
    subscription = bus.subscribe("event.*")
    engine = EventEngine(
        config,
        cast(Database, db),
        bus,
        snapshot_fn=snapshot_fn,
        media_dir=media_dir,
        spec=CascadeSpec(),
    )
    return engine, db, bus, subscription


async def approach_the_door(engine: EventEngine, camera: str = "front-door") -> float:
    """Sidewalk → porch → door in matcher-friendly steps; returns the last timestamp."""
    steps = [(0.5, 0.9), (0.5, 0.72), (0.5, 0.55), (0.5, 0.4), (0.5, 0.25)]
    ts = T0
    for index, (x, y) in enumerate(steps):
        ts = T0 + index
        await engine.on_detections(camera, ts, [person(x, y)], [])
    return ts


# --- pure rule helpers --------------------------------------------------------------------------


def make_track(
    *,
    zones: tuple[str, ...] = (),
    dwell_s: float = 0.0,
    approach: float | None = None,
    touch: bool = False,
    loiter: bool = False,
    repeat_pass: int = 0,
) -> TrackState:
    return TrackState(
        track_id=1,
        label="person",
        bbox=BBox(0.4, 0.4, 0.2, 0.4),
        at=datetime.fromtimestamp(T0, tz=UTC),
        velocity=(0.0, 0.0),
        dwell_s=dwell_s,
        zones=zones,
        approach=approach,
        loiter=loiter,
        repeat_pass=repeat_pass,
        touch=touch,
    )


ZONE_KINDS = {
    "sidewalk": ZoneKind.public,
    "porch": ZoneKind.private,
    "door": ZoneKind.entry,
}


def test_suppression_public_only_and_unzoned() -> None:
    assert is_suppressed(make_track(zones=("sidewalk",)), ZONE_KINDS, True)
    assert is_suppressed(make_track(zones=()), ZONE_KINDS, True)
    assert not is_suppressed(make_track(zones=()), ZONE_KINDS, False)  # no public zone at all
    assert not is_suppressed(make_track(zones=("sidewalk", "porch")), ZONE_KINDS, True)
    assert not is_suppressed(make_track(zones=("door",)), ZONE_KINDS, True)


def test_promotion_reasons_by_sensitivity() -> None:
    spec = CascadeSpec()
    touchy = make_track(zones=("door",), touch=True)
    entering = make_track(zones=("door",))
    dweller = make_track(zones=("porch",), dwell_s=11.0)
    half_dweller = make_track(zones=("porch",), dwell_s=6.0)
    pacer = make_track(zones=("porch",), loiter=True)
    caser = make_track(zones=("sidewalk", "porch"), repeat_pass=3)

    balanced = Sensitivity.balanced
    assert promotion_reason(touchy, ZONE_KINDS, spec, balanced) == "touch"
    assert promotion_reason(entering, ZONE_KINDS, spec, balanced) == "entry_zone"
    assert promotion_reason(dweller, ZONE_KINDS, spec, balanced) == "dwell"
    assert promotion_reason(half_dweller, ZONE_KINDS, spec, balanced) is None
    assert promotion_reason(pacer, ZONE_KINDS, spec, balanced) == "loiter"
    assert promotion_reason(caser, ZONE_KINDS, spec, balanced) == "repeat_pass"

    relaxed = Sensitivity.relaxed
    assert promotion_reason(touchy, ZONE_KINDS, spec, relaxed) == "touch"
    assert promotion_reason(entering, ZONE_KINDS, spec, relaxed) is None  # entry alone: no
    entry_dweller = make_track(zones=("door",), dwell_s=11.0)
    assert promotion_reason(entry_dweller, ZONE_KINDS, spec, relaxed) == "entry_dwell"
    assert promotion_reason(pacer, ZONE_KINDS, spec, relaxed) is None
    assert promotion_reason(caser, ZONE_KINDS, spec, relaxed) is None

    paranoid = Sensitivity.paranoid
    assert promotion_reason(half_dweller, ZONE_KINDS, spec, paranoid) == "dwell"  # 6 > 10 × 0.5
    assert promotion_reason(pacer, ZONE_KINDS, spec, paranoid) == "loiter"


# --- scripted scenes ----------------------------------------------------------------------------


async def test_public_only_walker_never_creates_an_event(tmp_path: Path, media_dir: Path) -> None:
    engine, db, _bus, subscription = make_engine(tmp_path, media_dir)
    for index in range(12):
        await engine.on_detections(
            "front-door", T0 + index, [person(0.15 + 0.05 * index, 0.92)], []
        )
    assert db.rows == {}
    assert subscription._queue.empty()


async def test_entry_zone_approach_confirms_and_publishes_canonical_payload(
    tmp_path: Path, media_dir: Path
) -> None:
    engine, db, _bus, subscription = make_engine(tmp_path, media_dir)
    confirm_ts = await approach_the_door(engine)

    assert len(db.rows) == 1
    row = next(iter(db.rows.values()))
    assert row["state"] == "confirmed"
    assert row["camera"] == "front-door"
    assert row["kinds"] == ["person"]
    assert row["zones"] == ["door"]
    assert row["policy"] == "default"  # built-in policy when none are configured
    assert row["started_at"] == pytest.approx(confirm_ts)

    topic, payload = await asyncio.wait_for(subscription.get(), timeout=1.0)
    assert topic == "event.confirmed"
    assert set(payload) == {
        "event",
        "id",
        "camera",
        "started_at",
        "ended_at",
        "kinds",
        "zones",
        "geometry",
        "summary",
        "intent",
        "policy",
        "media",
    }
    assert payload["event"] == "event.confirmed"
    assert payload["id"] == row["id"]
    assert payload["camera"] == "front-door"
    expected_iso = datetime.fromtimestamp(confirm_ts, tz=UTC).isoformat().replace("+00:00", "Z")
    assert payload["started_at"] == expected_iso
    assert payload["ended_at"] is None
    assert payload["kinds"] == ["person"]
    assert payload["zones"] == ["door"]
    assert set(payload["geometry"]) == {"approach", "dwell_s", "touch", "loiter", "repeat_pass"}
    assert payload["geometry"]["approach"] is not None and payload["geometry"]["approach"] > 0
    assert payload["geometry"]["dwell_s"] > 0  # entered the porch on the way in
    assert payload["summary"] is None and payload["intent"] is None  # honest pre-VLM nulls
    assert payload["policy"] == "default"
    assert payload["media"] == {
        "snapshot": f"/api/v1/events/{row['id']}/snapshot.jpeg",
        "clip": f"/api/v1/events/{row['id']}/clip.mp4",
    }

    snapshot = media_dir / "front-door" / "events" / row["id"] / "snapshot.jpeg"
    assert snapshot.is_file()
    assert snapshot.read_bytes().startswith(b"\xff\xd8")
    assert row["snapshot_path"] == str(snapshot)


async def test_engine_survives_snapshot_failure(tmp_path: Path, media_dir: Path) -> None:
    engine, db, _bus, subscription = make_engine(tmp_path, media_dir, snapshot_fn=snapshot_boom)
    await approach_the_door(engine)

    row = next(iter(db.rows.values()))
    assert row["state"] == "confirmed"  # the event survived the snapshot failure
    assert row["snapshot_path"] is None
    topic, payload = await asyncio.wait_for(subscription.get(), timeout=1.0)
    assert topic == "event.confirmed"
    assert payload["media"]["snapshot"] is None
    assert not (media_dir / "front-door" / "events").exists()


async def test_event_ends_after_absence(tmp_path: Path, media_dir: Path) -> None:
    engine, db, _bus, subscription = make_engine(tmp_path, media_dir)
    confirm_ts = await approach_the_door(engine)
    await asyncio.wait_for(subscription.get(), timeout=1.0)  # drain event.confirmed

    # Nothing for 16 s: the track dies (max_age 3 s), then the event closes (> 10 s gone).
    end_ts = confirm_ts + 16.0
    await engine.on_detections("front-door", end_ts, [], [])

    row = next(iter(db.rows.values()))
    assert row["ended_at"] == pytest.approx(end_ts)
    topic, payload = await asyncio.wait_for(subscription.get(), timeout=1.0)
    assert topic == "event.ended"
    assert payload["event"] == "event.ended"
    assert payload["id"] == row["id"]
    assert payload["ended_at"] == (
        datetime.fromtimestamp(end_ts, tz=UTC).isoformat().replace("+00:00", "Z")
    )


async def test_standalone_tick_closes_open_events(tmp_path: Path, media_dir: Path) -> None:
    engine, db, _bus, subscription = make_engine(tmp_path, media_dir)
    confirm_ts = await approach_the_door(engine)
    await asyncio.wait_for(subscription.get(), timeout=1.0)

    await engine.tick(confirm_ts + 5.0)  # too early — still open
    assert next(iter(db.rows.values()))["ended_at"] is None
    await engine.tick(confirm_ts + 11.0)
    assert next(iter(db.rows.values()))["ended_at"] == pytest.approx(confirm_ts + 11.0)
    topic, _payload = await asyncio.wait_for(subscription.get(), timeout=1.0)
    assert topic == "event.ended"


async def test_relaxed_policy_dismisses_baseline_promotion(tmp_path: Path, media_dir: Path) -> None:
    """A loiterer promotes on the balanced geometry, but the only configured policy is
    relaxed → the event lands dismissed: persisted, searchable, silent."""
    engine, db, _bus, subscription = make_engine(
        tmp_path,
        media_dir,
        policies=[
            {"name": "chill", "description": "only touch or entry dwell", "sensitivity": "relaxed"}
        ],
    )
    for index in range(13):  # pacing inside the porch (private)
        x = 0.45 if index % 2 == 0 else 0.55
        await engine.on_detections("front-door", T0 + index, [person(x, 0.5)], [])

    assert len(db.rows) == 1
    row = next(iter(db.rows.values()))
    assert row["state"] == "dismissed"
    assert row["policy"] is None
    assert subscription._queue.empty()  # dismissed events are silent
    assert not (media_dir / "front-door" / "events").exists()  # and get no snapshot


async def test_one_open_event_per_camera_extends_instead_of_duplicating(
    tmp_path: Path, media_dir: Path
) -> None:
    engine, db, _bus, _subscription = make_engine(tmp_path, media_dir)
    confirm_ts = await approach_the_door(engine)
    # Keep standing at the door: further promotions extend the open event.
    for offset in (1.0, 2.0, 3.0):
        await engine.on_detections("front-door", confirm_ts + offset, [person(0.5, 0.25)], [])
    assert len(db.rows) == 1


async def test_engine_contains_crashes(tmp_path: Path, media_dir: Path) -> None:
    engine, db, _bus, subscription = make_engine(tmp_path, media_dir)
    db.insert_error = RuntimeError("disk on fire")
    await approach_the_door(engine)  # must not raise into the pipeline
    assert db.rows == {}
    assert subscription._queue.empty()

    # Unknown cameras are ignored, not fatal.
    await engine.on_detections("ghost-cam", T0, [person(0.5, 0.5)], [])


# --- snapshot retry -------------------------------------------------------------------------


class FlakySnapshot:
    """Fails the first `fail_times` calls, then returns JPEG bytes; counts every call."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    async def __call__(self, camera: str) -> bytes:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("gateway still warming up")
        return b"\xff\xd8\xff\xe0-retried-" + camera.encode()


async def test_snapshot_retry_recovers_while_event_is_open(tmp_path: Path, media_dir: Path) -> None:
    """Promotion raced the gateway warmup → later passes retry and attach the snapshot."""
    flaky = FlakySnapshot(fail_times=2)
    engine, db, _bus, _subscription = make_engine(tmp_path, media_dir, snapshot_fn=flaky)
    confirm_ts = await approach_the_door(engine)  # attempt 1 fails at confirmation time
    (event_id,) = db.rows  # exactly one event was created
    assert flaky.calls == 1
    assert db.rows[event_id]["snapshot_path"] is None

    # Too soon (< 5 s since the last attempt): this pass must not retry.
    await engine.on_detections("front-door", confirm_ts + 2.0, [person(0.5, 0.25)], [])
    assert flaky.calls == 1

    # 5 s elapsed → a bare tick retries too (attempt 2, still failing)...
    await engine.tick(confirm_ts + 5.0)
    assert flaky.calls == 2
    assert db.rows[event_id]["snapshot_path"] is None

    # ...person still at the door keeps the event open; +7 is again too soon.
    await engine.on_detections("front-door", confirm_ts + 7.0, [person(0.5, 0.25)], [])
    assert flaky.calls == 2

    # Attempt 3 succeeds: file on disk, DB updated, in-memory media state updated.
    await engine.on_detections("front-door", confirm_ts + 10.0, [person(0.5, 0.25)], [])
    assert flaky.calls == 3
    snapshot = media_dir / "front-door" / "events" / event_id / "snapshot.jpeg"
    assert snapshot.is_file()
    assert snapshot.read_bytes() == b"\xff\xd8\xff\xe0-retried-front-door"
    assert db.rows[event_id]["snapshot_path"] == str(snapshot)

    # Once attached, later passes leave the gateway alone.
    await engine.on_detections("front-door", confirm_ts + 16.0, [person(0.5, 0.25)], [])
    assert flaky.calls == 3


async def test_snapshot_retry_gives_up_after_attempt_cap(tmp_path: Path, media_dir: Path) -> None:
    """A dead gateway gets SNAPSHOT_MAX_ATTEMPTS tries, then the engine stops asking."""
    flaky = FlakySnapshot(fail_times=99)  # never succeeds
    engine, db, _bus, _subscription = make_engine(tmp_path, media_dir, snapshot_fn=flaky)
    confirm_ts = await approach_the_door(engine)  # attempt 1
    for step in range(1, 8):  # 7 eligible passes at exactly 5 s spacing; only 4 may retry
        await engine.on_detections("front-door", confirm_ts + 5.0 * step, [person(0.5, 0.25)], [])
    assert flaky.calls == SNAPSHOT_MAX_ATTEMPTS
    row = next(iter(db.rows.values()))
    assert row["state"] == "confirmed"  # the event itself is fine, just snapshotless
    assert row["snapshot_path"] is None
    assert not (media_dir / "front-door" / "events").exists()


# --- retention class upgrade on close -------------------------------------------------------


async def test_close_of_confirmed_event_upgrades_footage_retention(
    tmp_path: Path, media_dir: Path
) -> None:
    """event.ended pulls overlapping continuous/motion segments (±5 s) into `event`."""
    engine, db, _bus, subscription = make_engine(tmp_path, media_dir)
    confirm_ts = await approach_the_door(engine)
    await asyncio.wait_for(subscription.get(), timeout=1.0)  # drain event.confirmed
    end_ts = confirm_ts + 16.0
    db.segments = [
        seg(1, confirm_ts - 30.0, confirm_ts - 20.0),  # ends before the pre-roll: untouched
        seg(2, confirm_ts - 10.0, confirm_ts - 4.0),  # overlaps the −5 s pre-roll
        seg(3, confirm_ts, confirm_ts + 10.0, klass="motion"),  # motion upgrades too
        seg(4, end_ts + 4.0, end_ts + 10.0),  # overlaps the +5 s post-roll
        seg(5, end_ts + 10.0, end_ts + 20.0),  # starts after the post-roll: untouched
        seg(6, confirm_ts, confirm_ts + 10.0, klass="favorite"),  # only_from guard holds
    ]

    await engine.on_detections("front-door", end_ts, [], [])

    assert db.upgrade_calls == [
        ("front-door", confirm_ts - 5.0, end_ts + 5.0, "event", ("continuous", "motion"))
    ]
    assert [segment.klass for segment in db.segments] == [
        "continuous",
        "event",
        "event",
        "event",
        "continuous",
        "favorite",
    ]
    topic, _payload = await asyncio.wait_for(subscription.get(), timeout=1.0)
    assert topic == "event.ended"  # the upgrade rides the event.ended path


async def test_close_of_dismissed_event_leaves_footage_on_continuous_schedule(
    tmp_path: Path, media_dir: Path
) -> None:
    """Dismissed events stay searchable, but never spend the `event` retention budget."""
    engine, db, _bus, subscription = make_engine(
        tmp_path,
        media_dir,
        policies=[
            {"name": "chill", "description": "only touch or entry dwell", "sensitivity": "relaxed"}
        ],
    )
    last_ts = T0
    for index in range(13):  # pacing inside the porch → loiter promotes, no policy wants it
        x = 0.45 if index % 2 == 0 else 0.55
        last_ts = T0 + index
        await engine.on_detections("front-door", last_ts, [person(x, 0.5)], [])
    row = next(iter(db.rows.values()))
    assert row["state"] == "dismissed"
    db.segments = [seg(1, T0, T0 + 30.0), seg(2, T0 + 5.0, T0 + 15.0, klass="motion")]

    await engine.tick(last_ts + 16.0)

    assert row["ended_at"] == pytest.approx(last_ts + 16.0)  # closed…
    assert db.upgrade_calls == []  # …but no retention upgrade
    assert [segment.klass for segment in db.segments] == ["continuous", "motion"]
    assert subscription._queue.empty()  # and still silent


# --- real Database round-trip (exercises the V2 migration + event methods) ----------------------


async def test_database_event_methods_roundtrip(tmp_path: Path) -> None:
    db = Database(tmp_path / "events-roundtrip.db")
    await db.connect()
    try:
        geometry = {
            "approach": 0.9,
            "dwell_s": 12.5,
            "touch": True,
            "loiter": False,
            "repeat_pass": 1,
        }
        await db.insert_event(
            "ev-a",
            "front-door",
            T0,
            "confirmed",
            ["person"],
            ["door"],
            geometry,
            policy="default",
        )
        await db.insert_event("ev-b", "front-door", T0 + 10, "dismissed", ["person"], [], {})
        await db.insert_event("ev-c", "garage", T0 + 20, "confirmed", ["vehicle"], ["drive"], {})

        rows = await db.list_events()
        assert [row.id for row in rows] == ["ev-c", "ev-b", "ev-a"]  # newest first
        assert [row.id for row in await db.list_events(camera="front-door")] == ["ev-b", "ev-a"]
        assert [row.id for row in await db.list_events(since_ts=T0 + 5)] == ["ev-c", "ev-b"]
        assert [row.id for row in await db.list_events(limit=1)] == ["ev-c"]

        row = await db.get_event("ev-a")
        assert row is not None
        assert row.kinds == ["person"] and row.zones == ["door"]  # JSON decoded
        assert row.geometry == geometry
        assert row.intent is None and row.feedback is None and row.ended_at is None
        assert row.policy == "default"
        assert await db.get_event("nope") is None

        await db.update_event("ev-a", ended_at=T0 + 42.0, snapshot_path="/media/x/snap.jpeg")
        updated = await db.get_event("ev-a")
        assert updated is not None
        assert updated.ended_at == pytest.approx(T0 + 42.0)
        assert updated.snapshot_path == "/media/x/snap.jpeg"
        assert updated.state == "confirmed"  # untouched by the partial update
        await db.update_event("ev-a")  # no fields: a no-op, not an error

        assert await db.set_event_feedback("ev-a", "up") is True
        refetched = await db.get_event("ev-a")
        assert refetched is not None and refetched.feedback == "up"
        assert await db.set_event_feedback("nope", "down") is False
    finally:
        await db.close()
