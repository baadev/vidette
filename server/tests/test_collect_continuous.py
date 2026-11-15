"""`vidette collect --continuous`: ring, motion gate, clips cut at segment joins, disk limits.

Pure parts (masks, the detector, cut plans, ring trimming) are unit-tested on numbers.
The rest runs the real ffmpeg: segments built from a test stream, and the whole collector
against `tests/fakes/rtsp_camera.py` streaming a still "street" with a box crossing it
and a blinking clock in the overlay corner.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fakes.rtsp_camera import FakeRtspCamera, encode_street_stream

from vidette.__main__ import main
from vidette.collect.clips import ClipMeta, finalize
from vidette.collect.collector import CollectSettings
from vidette.collect.continuous import (
    ContinuousCollector,
    ContinuousSettings,
    clip_window,
    continuous_command,
)
from vidette.collect.disk import MB, DiskLimits
from vidette.collect.motion import (
    OVERLAY_MASK,
    FrameScorer,
    MaskError,
    MotionDetector,
    MotionEvent,
    MotionSettings,
    Region,
    parse_fraction,
)
from vidette.collect.ring import Ring, Segment, cut, name_time, output_pattern, plan_cut
from vidette.collect.target import RtspTarget
from vidette.ffmpeg import probe

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg"
)

PASSWORD = "s3cr3t-Pa55"
W, H = 256, 144


# --- masks and the frame scorer --------------------------------------------------------


def test_region_parses_fractions_and_percentages() -> None:
    assert Region.parse("0.6,0,0.4,0.1") == Region(0.6, 0.0, 0.4, 0.1)
    assert Region.parse("60%, 0, 40%, 10%") == Region(0.6, 0.0, 0.4, 0.1)
    assert Region(0.6, 0.0, 0.4, 0.1).pixels(W, H) == (153, 0, 256, 15)
    for bad in ("1,2", "a,0,1,1", "0.9,0,0.5,0.1", "0,0,0,0.1"):
        with pytest.raises(MaskError):
            Region.parse(bad)
    assert parse_fraction("0.1%") == pytest.approx(0.001)
    assert parse_fraction("0.4") == 0.4
    with pytest.raises(ValueError):
        parse_fraction("150%")


def frame(*boxes: tuple[int, int, int, int], level: int = 200) -> bytes:
    """A 256×144 difference image with `level` inside the given (x, y, w, h) boxes."""
    pixels = bytearray(W * H)
    for x, y, w, h in boxes:
        for row in range(y, y + h):
            pixels[row * W + x : row * W + x + w] = bytes([level]) * w
    return bytes(pixels)


CLOCK = (200, 3, 40, 8)  # the seconds ticking inside the top-right overlay
PERSON = (60, 80, 8, 24)  # someone on the pavement


def test_scorer_ignores_the_overlay_and_small_changes() -> None:
    masked = FrameScorer(MotionSettings())
    unmasked = FrameScorer(MotionSettings(masks=()))
    assert masked.area(frame(CLOCK)) == 0.0
    assert unmasked.area(frame(CLOCK)) == pytest.approx(320 / (W * H))
    assert masked.area(frame(PERSON)) == pytest.approx(192 / masked.unmasked_pixels)
    assert masked.area(frame(PERSON, level=20)) == 0.0  # below the threshold: noise
    with pytest.raises(MaskError):
        FrameScorer(MotionSettings(masks=(Region(0, 0, 1, 0.95),)))


def feed(detector: MotionDetector, areas: list[float], start: float = 1000.0) -> list[MotionEvent]:
    step = 1 / detector.settings.fps
    events = []
    for index, area in enumerate(areas):
        event = detector.feed(area, start + index * step)
        if event is not None:
            events.append(event)
    return events


def test_the_clock_alone_never_opens_an_event_but_would_without_the_mask() -> None:
    settings = MotionSettings()
    ticking = [frame(CLOCK) if n % 4 == 0 else frame() for n in range(4 * 300)]  # 5 min
    for masks, expected_events in (((OVERLAY_MASK,), 0), ((), 1)):
        scorer = FrameScorer(MotionSettings(masks=masks))
        detector = MotionDetector(settings, max_event_s=60)
        events = feed(detector, [scorer.area(diff) for diff in ticking])
        assert len(events) == expected_events, masks
        if expected_events:
            # Without the mask the clock is "motion" forever: one capped event, then the
            # detector waits for a quiet that never comes.
            assert events[0].capped


def test_blips_and_light_changes_are_not_events_real_motion_is() -> None:
    settings = MotionSettings()  # 4 fps, 0.1 % to 40 %, 1 s to open, 2 s gap
    detector = MotionDetector(settings, max_event_s=114)
    quiet, moving, light = 0.0, 0.01, 0.9
    areas = (
        [quiet] * 8
        + [moving] * 2  # a leaf flicks: 0.5 s of motion
        + [quiet] * 12
        + [light] * 3  # headlights sweep the wall
        + [quiet] * 12
        + [moving] * 12  # a person walks through: 3 s
        + [quiet] * 4  # stops at the gate for 1 s (shorter than the gap)
        + [moving] * 8  # walks on: 2 s
        + [quiet] * 12
    )
    events = feed(detector, areas, start=0.0)
    assert len(events) == 1
    (event,) = events
    assert event.start == pytest.approx(37 * 0.25)  # first frame of the walk
    assert event.end == pytest.approx((37 + 12 + 4 + 8 - 1) * 0.25)
    assert event.active_s == pytest.approx(5.0) and not event.capped
    assert detector.light_frames == 3


def test_motion_longer_than_the_cap_gives_one_capped_event_then_waits_for_quiet() -> None:
    detector = MotionDetector(MotionSettings(), max_event_s=10)
    events = feed(detector, [0.02] * 4 * 60 + [0.0] * 12 + [0.02] * 8 + [0.0] * 12)
    assert [event.capped for event in events] == [True, False]
    assert events[0].duration_s == pytest.approx(10, abs=0.3)


def test_flush_closes_an_open_event_and_drops_a_candidate() -> None:
    detector = MotionDetector(MotionSettings(), max_event_s=60)
    feed(detector, [0.02] * 6)
    event = detector.flush()
    assert event is not None and event.active_s == pytest.approx(1.5)
    feed(detector, [0.02] * 2)
    assert detector.flush() is None


def test_clip_window_adds_rolls_and_stays_within_min_and_max() -> None:
    event = MotionEvent(start=100.0, end=101.0, active_s=1.0, peak_area=0.01, capped=False)
    assert clip_window(event, preroll_s=3, postroll_s=3, min_s=3, max_s=120) == (97.0, 104.0)
    assert clip_window(event, preroll_s=0, postroll_s=0, min_s=3, max_s=120) == (100.0, 103.0)
    long = MotionEvent(start=100.0, end=400.0, active_s=300.0, peak_area=0.01, capped=True)
    assert clip_window(long, preroll_s=3, postroll_s=3, min_s=3, max_s=120) == (97.0, 217.0)


# --- the ring: names, cut plans, trimming ----------------------------------------------


def test_segment_names_carry_the_utc_offset_and_percent_signs_are_escaped() -> None:
    stamp = name_time("20261114T153742+0200.mkv")
    assert stamp is not None and time.gmtime(stamp)[3:6] == (13, 37, 42)
    assert name_time("notes.txt") is None
    assert output_pattern(Path("/data/100%/ring")) == (
        "/data/100%%/ring/%Y%m%dT%H%M%S%z.mkv"
    )


def seg(tmp: Path, start: float, duration: float | None = 60.0, size: int = 1000) -> Segment:
    path = tmp / f"{start:.0f}.mkv"
    path.write_bytes(b"x" * size)
    return Segment(path, start, duration, size)


def test_a_cut_across_a_segment_join_uses_both_segments(tmp_path: Path) -> None:
    ring = [seg(tmp_path, t) for t in (1000.0, 1060.0, 1120.0)] + [seg(tmp_path, 1180, None)]
    pieces = plan_cut(ring, 1057.0, 1066.0)
    assert [piece.segment.start for piece in pieces] == [1000.0, 1060.0]
    assert pieces[0].inpoint == pytest.approx(57.0) and pieces[0].outpoint is None
    assert pieces[1].inpoint is None and pieces[1].outpoint == pytest.approx(6.0)
    assert pieces[0].wall_start == 1057.0 and pieces[1].wall_end == 1066.0
    # Inside one segment: both points on it. Past the finished ones: nothing to cut yet.
    (inside,) = plan_cut(ring, 1130.0, 1140.0)
    assert (inside.inpoint, inside.outpoint) == (10.0, 20.0)
    assert plan_cut(ring, 1185.0, 1190.0) == []


def test_pre_roll_never_reaches_across_a_stream_restart(tmp_path: Path) -> None:
    ring = [seg(tmp_path, 1000.0), seg(tmp_path, 1075.0)]  # 15 s gap: reconnect
    pieces = plan_cut(ring, 1058.0, 1080.0)
    assert [piece.segment.start for piece in pieces] == [1075.0]
    assert pieces[0].inpoint is None


def test_trim_keeps_size_age_open_segment_pending_clips_and_a_minimum(tmp_path: Path) -> None:
    now = 10_000.0
    ring = [seg(tmp_path, 1000.0 + 60 * n, size=100) for n in range(10)]
    ring.append(seg(tmp_path, 1600.0, None, size=100))  # being written
    store = Ring(tmp_path)
    deleted = store.trim(ring, max_bytes=550, max_age_s=10**9, now=now, min_keep=3)
    assert [s.start for s in deleted] == [1000.0 + 60 * n for n in range(6)]
    assert all(not s.path.exists() for s in deleted)
    rest = ring[6:]
    # A clip waiting for 1390 s… protects that segment and everything after it.
    assert store.trim(rest, max_bytes=0, max_age_s=10**9, now=now, keep_from=1390.0) == []
    # Age: everything older than a minute, except the last three (one of them open).
    gone = store.trim(rest, max_bytes=10**9, max_age_s=60, now=now, min_keep=3)
    assert [s.start for s in gone] == [1360.0, 1420.0]


@needs_ffmpeg
async def test_cutting_at_a_segment_join_copies_from_both(tmp_path: Path) -> None:
    stream = encode_street_stream(tmp_path / "street.h264", seconds=8)
    ring_dir = tmp_path / "ring"
    ring_dir.mkdir()
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-fflags", "+genpts", "-r", "10", "-i", str(stream), "-map", "0:v:0", "-c", "copy",
            "-f", "segment", "-segment_time", "2", "-segment_format", "matroska",
            "-reset_timestamps", "1", str(ring_dir / "%03d.mkv"),
        ],
        check=True,
    )  # fmt: skip
    files = sorted(ring_dir.glob("*.mkv"))
    assert len(files) == 4
    segments, start = [], 5000.0
    for path in files:
        info = await probe(path)
        segments.append(Segment(path, start, info.duration_s, path.stat().st_size))
        start += info.duration_s
    pieces = plan_cut(segments, 5001.0, 5005.5)  # from the 1st segment into the 3rd
    assert len(pieces) == 3
    out = tmp_path / "clip.mkv"
    await cut(pieces, out)
    info = await probe(out)
    # Starts on the key frame at or before 1.0 s (one per second here): 4.5 s of video.
    assert 4.3 <= info.duration_s <= 5.6, info.duration_s
    assert not list(tmp_path.glob("*.ffconcat"))


# --- rotation --------------------------------------------------------------------------


@needs_ffmpeg
async def test_rotation_turns_key_frames_and_tags_the_mp4(tmp_path: Path) -> None:
    part = tmp_path / ".partial" / "1.mkv.part"
    part.parent.mkdir()
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=320x240:r=10",
            "-vf", "drawbox=x=240:y=0:w=80:h=40:color=white:t=fill",  # the overlay corner
            "-t", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "matroska", str(part),
        ],
        check=True,
    )  # fmt: skip
    meta = await finalize(
        part, tmp_path, camera="street", started_at=time.time(), stop_reason="motion",
        min_seconds=1, rotation=90,
    )  # fmt: skip
    assert meta is not None and meta.rotation == 90 and (meta.width, meta.height) == (320, 240)
    first = tmp_path / meta.frames[0]
    gray = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(first),
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    assert len(gray) == 240 * 320  # portrait now
    width = 240
    bright = [(i % width, i // width) for i, value in enumerate(gray) if value > 128]
    xs, ys = [p[0] for p in bright], [p[1] for p in bright]
    # Turned clockwise: the top-right corner of the camera's picture is now bottom-right.
    assert min(xs) >= 190 and min(ys) >= 230
    side_data = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream_side_data=rotation",
         "-of", "csv=p=0", str(tmp_path / meta.clip)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert "-90" in side_data or side_data.strip() == ""  # tagged where ffmpeg can


# --- the whole collector against a fake continuous camera ------------------------------


@pytest.fixture(scope="module")
def street(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg")
    return encode_street_stream(tmp_path_factory.mktemp("street") / "street.h264")


def settings_for(dataset: Path, **overrides: object) -> CollectSettings:
    values: dict[str, object] = {
        "dataset": dataset,
        "camera": "street",
        "min_seconds": 1.0,
        "max_seconds": 30.0,
        "silence_s": 3.0,
        "busy_backoff_s": (0.4, 1.6),
        "silent_backoff_s": (0.4, 1.6),
        "disk_recheck_s": 0.5,
        "limits": DiskLimits(max_dataset_bytes=200 * MB, min_free_bytes=1),
        "jitter": 0.0,
        "rotation": 90,
    }
    values.update(overrides)
    return CollectSettings(**values)  # type: ignore[arg-type]


def continuous_for(**overrides: object) -> ContinuousSettings:
    values: dict[str, object] = {
        "segment_s": 2.0,
        "ring_max_bytes": 1,  # as small as it gets: the ring keeps its minimum
        "preroll_s": 1.0,
        "postroll_s": 1.0,
        "motion": MotionSettings(min_motion_s=0.5, gap_s=1.0),
        "hwaccel": None,
        "stall_s": 5.0,
        "reconnect_s": (0.5, 2.0),
    }
    values.update(overrides)
    return ContinuousSettings(**values)  # type: ignore[arg-type]


async def run_until(
    collector: ContinuousCollector, stop: asyncio.Event, condition: object, timeout: float
) -> None:
    task = asyncio.create_task(collector.run(stop))
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline and not task.done():
            if callable(condition) and condition():
                break
            await asyncio.sleep(0.1)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=30)


@needs_ffmpeg
async def test_continuous_stream_fills_a_bounded_ring_and_cuts_one_clip_per_motion(
    tmp_path: Path, street: Path, caplog: pytest.LogCaptureFixture
) -> None:
    camera = FakeRtspCamera(street, credentials=("admin", PASSWORD))
    await camera.reserve_port()
    await camera.wake(120)
    target = RtspTarget.parse(camera.url("admin", PASSWORD), "test")
    dataset = tmp_path / "street"
    collector = ContinuousCollector(target, settings_for(dataset), continuous_for())
    stop = asyncio.Event()
    caplog.set_level(logging.INFO)
    ring_sizes: list[int] = []

    def first_clip() -> bool:
        ring_sizes.append(len(list((dataset / "ring").glob("*.mkv"))))
        return collector.stats.clips >= 1

    # The scene loops every 12 s with motion from 4 to 7 s: ~14 s to the first clip.
    await run_until(collector, stop, first_clip, timeout=40)
    await camera.sleep()

    sidecars = sorted((dataset / "clips").glob("*.json"))
    assert len(sidecars) == 1, caplog.text
    meta = ClipMeta.model_validate(json.loads(sidecars[0].read_text()))
    assert meta.trigger == "motion" and meta.rotation == 90 and meta.motion is not None
    assert meta.motion.peak_area > 0.005 and not meta.motion.capped
    # 3 s of motion, 1 s pre-roll, 1 s post-roll, up to a key frame (1 s) more in front.
    assert 3.5 <= meta.duration_s <= 7.0, meta.duration_s
    assert (dataset / meta.clip).suffix == ".mp4" and 4 <= len(meta.frames) <= 8
    jpg = await probe(dataset / meta.frames[0])
    assert (jpg.width, jpg.height) == (240, 320)  # key frames turned upright
    text = sidecars[0].read_text()
    assert PASSWORD not in text and "rtsp://" not in text
    # The clock blinked all along and never became an event; only the box did.
    assert collector.counters.events == 1
    # The ring stayed bounded: its minimum of 3 finished segments, the open one, and what
    # the pending clip protected; old segments were deleted.
    assert max(ring_sizes) <= 7
    names = sorted(p.name for p in (dataset / "ring").glob("*.mkv"))
    assert len(names) <= 5
    assert not list((dataset / ".partial").glob("*"))
    assert camera.stats.max_concurrent_sessions == 1 and camera.stats.unclean_disconnects == 0
    assert PASSWORD not in caplog.text
    state = json.loads((dataset / ".vidette" / "collect-state.json").read_text())
    assert state["clips"] == 1 and state["watched_seconds"] > 8


@needs_ffmpeg
async def test_an_early_window_of_a_motion_event_and_its_clip_share_an_id(
    tmp_path: Path, street: Path
) -> None:
    camera = FakeRtspCamera(street)
    await camera.reserve_port()
    await camera.wake(120)
    dataset = tmp_path / "street"
    collector = ContinuousCollector(
        RtspTarget.parse(camera.url(), "t"),
        settings_for(dataset, early_s=1.0),
        continuous_for(segment_s=30.0),  # the clip comes from the open segment
    )
    await run_until(collector, asyncio.Event(), lambda: collector.stats.clips >= 1, timeout=40)
    await camera.sleep()
    (early,) = (dataset / ".early").glob("*.json")
    (sidecar,) = (dataset / "clips").glob("*.json")
    assert early.stem == sidecar.stem
    early_meta = json.loads(early.read_text())
    assert (dataset / early_meta["clip"]).is_file()
    assert {"motion", "first_frame", "early"} <= set(early_meta["timeline"])
    meta = ClipMeta.model_validate(json.loads(sidecar.read_text()))
    assert meta.timeline["motion"] == early_meta["timeline"]["motion"]
    assert early_meta["timeline"]["early"] < meta.timeline["closed"]
    # cut from a 30 s segment still being written, not after it finished
    assert len(list((dataset / "ring").glob("*.mkv"))) == 1
    assert not list((dataset / ".partial").glob("*"))


@needs_ffmpeg
async def test_free_space_floor_blocks_before_connecting(tmp_path: Path, street: Path) -> None:
    camera = FakeRtspCamera(street)
    await camera.reserve_port()
    await camera.wake(60)
    collector = ContinuousCollector(
        RtspTarget.parse(camera.url(), "t"),
        settings_for(tmp_path / "d", limits=DiskLimits(min_free_bytes=10**15)),
        continuous_for(),
    )
    stop = asyncio.Event()
    await run_until(collector, stop, lambda: collector.state == "disk_full", timeout=10)
    await camera.sleep()
    assert camera.stats.connections == 0


@needs_ffmpeg
async def test_running_below_the_floor_stops_the_stream_cleanly(
    tmp_path: Path, street: Path, caplog: pytest.LogCaptureFixture
) -> None:
    camera = FakeRtspCamera(street)
    await camera.reserve_port()
    await camera.wake(60)
    real_free = shutil.disk_usage(tmp_path).free
    started = time.monotonic()

    def free(path: Path) -> int:
        return real_free if time.monotonic() - started < 5 else 0

    collector = ContinuousCollector(
        RtspTarget.parse(camera.url(), "t"),
        settings_for(tmp_path / "d", limits=DiskLimits(min_free_bytes=1)),
        continuous_for(),
        free=free,
    )
    stop = asyncio.Event()
    caplog.set_level(logging.INFO)
    await run_until(collector, stop, lambda: collector.state == "disk_full", timeout=20)
    await camera.sleep()
    assert collector.stats.outcomes.get("recorded") == 1
    assert camera.stats.teardowns == 1 and camera.stats.unclean_disconnects == 0
    assert "only 0 B free" in caplog.text


@needs_ffmpeg
async def test_a_full_clip_budget_drops_clips_but_the_ring_keeps_going(
    tmp_path: Path, street: Path, caplog: pytest.LogCaptureFixture
) -> None:
    camera = FakeRtspCamera(street)
    await camera.reserve_port()
    await camera.wake(60)
    dataset = tmp_path / "d"
    dataset.mkdir()
    (dataset / "old.bin").write_bytes(b"x" * 2_000_000)
    collector = ContinuousCollector(
        RtspTarget.parse(camera.url(), "t"),
        settings_for(dataset, limits=DiskLimits(max_dataset_bytes=1_000_000, min_free_bytes=1)),
        continuous_for(),
    )
    stop = asyncio.Event()
    caplog.set_level(logging.INFO)
    await run_until(collector, stop, lambda: collector.counters.dropped >= 1, timeout=40)
    await camera.sleep()
    assert collector.counters.dropped >= 1 and collector.stats.clips == 0
    assert list((dataset / "ring").glob("*.mkv"))
    assert "not kept" in caplog.text and "cap is" in caplog.text


@needs_ffmpeg
async def test_a_camera_that_refuses_gets_backoff(tmp_path: Path, street: Path) -> None:
    camera = FakeRtspCamera(street)
    await camera.reserve_port()
    await camera.wake(60)
    camera.make_busy()
    collector = ContinuousCollector(
        RtspTarget.parse(camera.url(), "t"), settings_for(tmp_path / "d"), continuous_for()
    )
    stop = asyncio.Event()
    await run_until(collector, stop, lambda: False, timeout=3.5)
    await camera.sleep()
    assert 2 <= camera.stats.rejected_busy <= 4
    assert collector.stats.outcomes == {"busy": camera.stats.rejected_busy}


@needs_ffmpeg
async def test_behind_a_base_station_404_means_asleep_and_is_checked_steadily(
    tmp_path: Path, street: Path, caplog: pytest.LogCaptureFixture
) -> None:
    camera = FakeRtspCamera(street)
    await camera.reserve_port()
    await camera.wake(120)  # the base station's port is always open
    camera.make_busy()  # …and it answers 404 while the camera sleeps
    collector = ContinuousCollector(
        RtspTarget.parse(camera.url(), "t"),
        settings_for(tmp_path / "d", base_station=True, probe_every_s=0.3),
        continuous_for(),
    )
    stop = asyncio.Event()
    caplog.set_level(logging.INFO)

    async def wakes_up() -> None:
        await asyncio.sleep(2.5)
        camera.make_busy(False)

    waking = asyncio.create_task(wakes_up())
    await run_until(collector, stop, lambda: collector.stats.clips >= 1, timeout=40)
    await waking
    await camera.sleep()
    # Checked every 0.3 s while asleep (a backoff would have allowed 2 or 3), logged once.
    assert camera.stats.rejected_busy >= 5
    assert caplog.text.count("camera asleep (the base station") == 1
    assert collector.stats.clips == 1


# --- command line ----------------------------------------------------------------------


def test_command_is_one_session_with_a_copied_ring_and_a_tiny_grey_branch() -> None:
    command = continuous_command(
        "ffmpeg", "rtsp://x", Path("/d/ring"), segment_s=60, silence_s=8,
        motion=MotionSettings(), hwaccel="videotoolbox",
    )  # fmt: skip
    joined = " ".join(command)
    assert command.count("-i") == 1 and "-hwaccel videotoolbox" in joined
    assert "-f segment -segment_time 60" in joined and "-c copy" in joined
    assert "/d/ring/%Y%m%dT%H%M%S%z.mkv" in joined
    assert "fps=4,scale=256:144" in joined and "tblend=all_mode=difference" in joined
    assert "-allowed_media_types video" in joined and "-nostdin" not in joined
    assert "-use_wallclock_as_timestamps" not in joined


def test_command_behind_a_restreamer_stamps_packets_on_arrival() -> None:
    command = continuous_command(
        "ffmpeg", "rtsp://127.0.0.1:8554/yard", Path("/d/ring"), segment_s=60, silence_s=8,
        motion=MotionSettings(), hwaccel=None, wallclock=True,
    )  # fmt: skip
    assert command.index("-use_wallclock_as_timestamps") < command.index("-i")


def test_cli_continuous_check_reports_ring_and_motion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg")
    monkeypatch.setenv("VIDETTE_RTSP_URL", f"rtsp://u:{PASSWORD}@127.0.0.1:9/live1")
    code = main([
        "collect", str(tmp_path / "d"), "--continuous", "--check", "--min-free", "1B",
        "--rotate", "270", "--mask", "0,0.5,0.2,0.5",
    ])  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0, out
    assert PASSWORD not in out and "ring     0 B of 3 GB" in out
    assert "masks: 0.6,0,0.4,0.1, 0,0.5,0.2,0.5" in out and "rotate 270°" in out
    code = main(["collect", str(tmp_path / "d"), "--check", "--base-station", "--min-free", "1B"])
    assert code == 0 and "closed: the base station is off" in capsys.readouterr().out


def test_cli_rejects_bad_continuous_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VIDETTE_RTSP_URL", "rtsp://cam/live1")
    base = ["collect", str(tmp_path / "d"), "--continuous", "--check"]
    with pytest.raises(SystemExit):
        main([*base, "--rotate", "45"])  # argparse: not one of 0, 90, 180, 270
    for extra in (["--mask", "1,2"], ["--motion-area", "50%"], ["--segment", "1s"]):
        assert main([*base, *extra]) == 2, extra
        assert "error:" in capsys.readouterr().err


def test_dataset_size_leaves_the_ring_to_its_own_cap(tmp_path: Path) -> None:
    from vidette.collect.disk import measure

    (tmp_path / "ring").mkdir()
    (tmp_path / "ring" / "a.mkv").write_bytes(b"x" * 5000)
    (tmp_path / "clips").mkdir()
    (tmp_path / "clips" / "b.mp4").write_bytes(b"x" * 700)
    limits = DiskLimits()
    assert measure(tmp_path, limits).dataset_bytes == 5700
    assert measure(tmp_path, limits, skip=("ring",)).dataset_bytes == 700
