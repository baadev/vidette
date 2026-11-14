"""`vidette collect`: secrets never leak, the disk guard holds, the camera is treated gently.

Unit tests cover the pure parts (URL redaction, sizes, budgets, backoff, ffmpeg's error
words). Integration tests run the real collector and the real ffmpeg against
`tests/fakes/rtsp_camera.py`, a localhost RTSP camera that sleeps, wakes, refuses second
clients and holds dead sessions like a battery camera did in v0.1.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path

import pytest
from fakes.rtsp_camera import FakeRtspCamera, encode_test_stream

from vidette.__main__ import main
from vidette.collect.clips import ClipMeta, adopt_leftovers, frame_count, partial_path
from vidette.collect.collector import (
    Collector,
    CollectSettings,
    DatasetLockedError,
    backoff,
    tcp_reachable,
)
from vidette.collect.disk import GB, MB, DiskLimits, DiskState
from vidette.collect.session import Outcome, classify, record_command
from vidette.collect.target import RtspTarget, TargetError, load_target
from vidette.units import format_size, parse_duration, parse_size

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg"
)

PASSWORD = "s3cr3t-Pa55"


# --- the URL and its secrets ------------------------------------------------------------


def test_target_redacts_and_scrubs_every_spelling_of_the_secret() -> None:
    target = RtspTarget.parse(f"rtsp://admin:{PASSWORD}@10.0.20.30/live0", "test")
    assert target.host == "10.0.20.30" and target.port == 554
    assert target.redacted == "rtsp://***@10.0.20.30:554/live0"
    assert PASSWORD not in repr(target) and PASSWORD not in str(target)
    ffmpeg_line = (
        f"Error opening input file rtsp://admin:{PASSWORD}@10.0.20.30/live0. "
        f"[tcp] auth admin:{PASSWORD} and {PASSWORD} again"
    )
    scrubbed = target.scrub(ffmpeg_line)
    assert PASSWORD not in scrubbed and "admin:" not in scrubbed
    assert "rtsp://***@10.0.20.30:554/live0" in scrubbed


def test_percent_encoded_password_is_scrubbed_in_both_forms() -> None:
    target = RtspTarget.parse("rtsp://u:p%40ss%3Aword@cam/live0", "test")
    assert "p@ss:word" not in target.scrub("decoded p@ss:word")
    assert "p%40ss%3Aword" not in target.scrub("encoded p%40ss%3Aword")


@pytest.mark.parametrize("bad", ["http://cam/live0", "rtsp:///live0", "not a url at all"])
def test_bad_urls_are_rejected_without_echoing_them(bad: str) -> None:
    with pytest.raises(TargetError) as excinfo:
        RtspTarget.parse(bad, "VIDETTE_RTSP_URL")
    assert bad not in str(excinfo.value)


def test_url_comes_from_env_first_then_a_private_file(tmp_path: Path) -> None:
    url_file = tmp_path / "rtsp-url"
    url_file.write_text(f"# the porch camera\nrtsp://u:{PASSWORD}@cam/live0\n")
    url_file.chmod(0o644)
    target, warnings = load_target(url_file=url_file, env={})
    assert target.host == "cam" and target.origin == str(url_file)
    assert warnings and "chmod 600" in warnings[0]
    url_file.chmod(0o600)
    assert load_target(url_file=url_file, env={})[1] == []
    env_target, _ = load_target(url_file=url_file, env={"VIDETTE_RTSP_URL": "rtsp://other/x"})
    assert env_target.host == "other"
    with pytest.raises(TargetError, match="VIDETTE_RTSP_URL"):
        load_target(url_file=tmp_path / "missing", env={})


# --- units, budgets, backoff -----------------------------------------------------------


def test_sizes_and_durations() -> None:
    assert parse_size("5GB") == 5 * GB
    assert parse_size("10GiB") == 10 * 1024**3
    assert parse_size("500MB") == 500 * MB
    assert parse_duration("90s") == 90 and parse_duration("2m") == 120
    assert parse_duration("1.5h") == 5400 and parse_duration("8") == 8
    assert format_size(5 * GB) == "5 GB"
    with pytest.raises(ValueError):
        parse_size("lots")


def test_disk_budget_halves_the_headroom_because_finalizing_briefly_doubles_a_clip() -> None:
    limits = DiskLimits(max_dataset_bytes=5 * GB, min_free_bytes=10 * GB)
    roomy = DiskState(dataset_bytes=1 * GB, free_bytes=50 * GB, limits=limits)
    assert roomy.blocked_reason is None
    assert roomy.clip_allowance_bytes == (4 * GB - 4 * MB) // 2
    tight_disk = DiskState(dataset_bytes=0, free_bytes=11 * GB, limits=limits)
    assert tight_disk.clip_allowance_bytes == (1 * GB - 4 * MB) // 2  # free space binds
    full_disk = DiskState(dataset_bytes=0, free_bytes=9 * GB, limits=limits)
    assert full_disk.blocked_reason is not None and "free" in full_disk.blocked_reason
    full_set = DiskState(dataset_bytes=5 * GB, free_bytes=50 * GB, limits=limits)
    assert full_set.blocked_reason is not None and "cap" in full_set.blocked_reason


def test_backoff_doubles_to_its_cap() -> None:
    assert [backoff(n, (30, 300)) for n in range(1, 7)] == [30, 60, 120, 240, 300, 300]


def test_frame_count_stays_between_four_and_eight() -> None:
    assert [frame_count(s) for s in (2, 10, 24, 40, 120)] == [4, 4, 5, 7, 8]


def test_record_command_is_video_only_bounded_and_probes_long() -> None:
    command = record_command(
        "ffmpeg", "rtsp://x", Path("/d/p.mkv.part"), max_seconds=120, silence_s=8, max_bytes=999
    )
    joined = " ".join(command)
    assert "-allowed_media_types video" in joined and "-c copy" in joined
    assert "-t 120" in joined and "-fs 999" in joined and "-timeout 8000000" in joined
    assert "-analyzeduration 10000000" in joined  # v0.1: a waking camera sends SPS late
    assert "-rtsp_transport tcp" in joined and "-nostdin" not in joined  # q needs stdin
    assert "-use_wallclock_as_timestamps" not in joined  # the camera's own clock by default


def test_record_command_behind_a_restreamer_stamps_packets_on_arrival() -> None:
    command = record_command(
        "ffmpeg", "rtsp://127.0.0.1:8554/door", Path("/d/p.mkv.part"),
        max_seconds=120, silence_s=8, max_bytes=999, wallclock=True,
    )  # fmt: skip
    # an input option: it must come before -i
    assert command.index("-use_wallclock_as_timestamps") < command.index("-i")
    assert command[command.index("-use_wallclock_as_timestamps") + 1] == "1"


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("method DESCRIBE failed: 404 (Stream Not Found)", Outcome.busy),
        ("method DESCRIBE failed: 401 (Unauthorized)", Outcome.unauthorized),
        ("Connection to tcp://10.0.20.5:554 failed: Connection refused", Outcome.asleep),
        ("Failed reading RTSP data: Operation timed out | dimensions not set", Outcome.silent),
        ("something new and strange", Outcome.failed),
    ],
)
def test_ffmpeg_words_map_to_outcomes(stderr: str, expected: Outcome) -> None:
    assert classify(stderr, media_seconds=0, bytes_written=0) is expected
    assert classify(stderr, media_seconds=3.2, bytes_written=1000) is Outcome.recorded


# --- the real thing: collector + ffmpeg + fake camera ----------------------------------


@pytest.fixture(scope="module")
def stream(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg")
    return encode_test_stream(tmp_path_factory.mktemp("stream") / "test.h264")


def fast_settings(dataset: Path, **overrides: object) -> CollectSettings:
    values: dict[str, object] = {
        "dataset": dataset,
        "camera": "porch",
        "min_seconds": 1.0,
        "max_seconds": 30.0,
        "probe_every_s": 0.2,
        "cooldown_s": 0.2,
        "silence_s": 1.5,
        "busy_backoff_s": (0.4, 1.6),
        "silent_backoff_s": (0.4, 1.6),
        "auth_retry_s": 30.0,
        "disk_recheck_s": 0.5,
        "limits": DiskLimits(max_dataset_bytes=200 * MB, min_free_bytes=1),
        "jitter": 0.0,
    }
    values.update(overrides)
    return CollectSettings(**values)  # type: ignore[arg-type]


async def run_until(
    collector: Collector, stop: asyncio.Event, condition: object, timeout: float
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
        await asyncio.wait_for(task, timeout=20)


@needs_ffmpeg
async def test_wake_periods_become_clips_with_frames_and_a_clean_sidecar(
    tmp_path: Path, stream: Path, caplog: pytest.LogCaptureFixture
) -> None:
    camera = FakeRtspCamera(stream, credentials=("admin", PASSWORD))
    await camera.reserve_port()
    target = RtspTarget.parse(camera.url("admin", PASSWORD), "test")
    dataset = tmp_path / "front-door"
    collector = Collector(target, fast_settings(dataset))
    stop = asyncio.Event()
    caplog.set_level(logging.INFO)

    async def motion() -> None:
        await asyncio.sleep(0.6)  # asleep first: only port checks, no RTSP
        assert camera.stats.connections == 0
        await camera.wake(3.0)
        await asyncio.sleep(4.0)
        await camera.wake(2.5)  # a second wake period, a second clip

    motion_task = asyncio.create_task(motion())
    await run_until(collector, stop, lambda: collector.stats.clips >= 2, timeout=25)
    await motion_task
    await camera.sleep()

    sidecars = sorted((dataset / "clips").glob("*.json"))
    assert len(sidecars) == 2, sorted(p.name for p in (dataset / "clips").iterdir())
    for sidecar in sidecars:
        text = sidecar.read_text()
        assert PASSWORD not in text and "admin" not in text and "rtsp://" not in text
        meta = ClipMeta.model_validate(json.loads(text))
        clip = dataset / meta.clip
        assert clip.suffix == ".mp4" and clip.is_file()
        assert 1.5 <= meta.duration_s <= 4.5
        assert meta.codec == "h264" and (meta.width, meta.height) == (320, 240)
        assert 4 <= len(meta.frames) <= 8
        assert all((dataset / frame).is_file() for frame in meta.frames)
        assert meta.camera == "porch" and meta.stop_reason.startswith("stream ended")
        assert len(meta.id) == len("20261114-210304")
    assert not list((dataset / ".partial").glob("*")), "partial files are cleaned up"
    # Gentle: one session per wake, and every stop the camera saw was a clean one.
    assert camera.stats.max_concurrent_sessions == 1
    assert camera.stats.unclean_disconnects == 0
    assert camera.stats.rejected_busy == 0
    assert PASSWORD not in caplog.text
    state = json.loads((dataset / ".vidette" / "collect-state.json").read_text())
    assert state["clips"] == 2 and state["watched_seconds"] > 5


@needs_ffmpeg
async def test_stop_mid_recording_tears_down_cleanly_and_keeps_the_clip(
    tmp_path: Path, stream: Path
) -> None:
    camera = FakeRtspCamera(stream, ghost_s=60)
    await camera.reserve_port()
    await camera.wake(60)
    collector = Collector(RtspTarget.parse(camera.url(), "t"), fast_settings(tmp_path / "d"))
    stop = asyncio.Event()

    def recording_for_a_while() -> bool:
        return camera.stats.frames_sent > 25

    await run_until(collector, stop, recording_for_a_while, timeout=15)
    await asyncio.sleep(0.5)
    assert camera.stats.teardowns == 1 and camera.stats.unclean_disconnects == 0
    clips = list((tmp_path / "d" / "clips").glob("*.mp4"))
    assert len(clips) == 1
    meta = json.loads(clips[0].with_suffix(".json").read_text())
    assert meta["stop_reason"] == "stopped by the collector"
    await camera.sleep()


@needs_ffmpeg
async def test_max_duration_cuts_a_long_wake_period(tmp_path: Path, stream: Path) -> None:
    camera = FakeRtspCamera(stream)
    await camera.reserve_port()
    await camera.wake(60)
    settings = fast_settings(tmp_path / "d", max_seconds=2.0, once=True)
    collector = Collector(RtspTarget.parse(camera.url(), "t"), settings)
    await asyncio.wait_for(collector.run(asyncio.Event()), timeout=20)
    await camera.sleep()
    (sidecar,) = (tmp_path / "d" / "clips").glob("*.json")
    meta = json.loads(sidecar.read_text())
    assert meta["stop_reason"] == "max duration" and meta["duration_s"] <= 2.6


@needs_ffmpeg
async def test_a_busy_camera_gets_exponential_backoff_not_a_reconnect_storm(
    tmp_path: Path, stream: Path
) -> None:
    camera = FakeRtspCamera(stream)
    await camera.reserve_port()
    await camera.wake(60)
    camera.make_busy()
    collector = Collector(RtspTarget.parse(camera.url(), "t"), fast_settings(tmp_path / "d"))
    stop = asyncio.Event()
    await run_until(collector, stop, lambda: False, timeout=3.5)
    await camera.sleep()
    # 0.4 + 0.8 + 1.6 s of backoff fit in 3.5 s: at most 4 DESCRIBEs, not dozens.
    assert 2 <= camera.stats.rejected_busy <= 4
    assert collector.stats.outcomes == {"busy": camera.stats.rejected_busy}
    assert not list((tmp_path / "d" / "clips").glob("*.mp4"))


@needs_ffmpeg
async def test_a_base_station_404_is_a_sleeping_camera_checked_every_probe(
    tmp_path: Path, stream: Path, caplog: pytest.LogCaptureFixture
) -> None:
    camera = FakeRtspCamera(stream)
    await camera.reserve_port()
    await camera.wake(60)
    camera.make_busy()
    settings = fast_settings(tmp_path / "d", base_station=True, probe_every_s=0.3)
    collector = Collector(RtspTarget.parse(camera.url(), "t"), settings)
    stop = asyncio.Event()
    caplog.set_level(logging.INFO)

    async def wakes_up() -> None:
        await asyncio.sleep(2.0)
        camera.make_busy(False)
        await asyncio.sleep(2.5)
        await camera.sleep()

    waking = asyncio.create_task(wakes_up())
    await run_until(collector, stop, lambda: collector.stats.clips >= 1, timeout=20)
    await waking
    assert camera.stats.rejected_busy >= 4  # steady checks, not 30 s … 5 min of backoff
    assert caplog.text.count("camera asleep (the base station") == 1
    assert "404 (another RTSP" not in caplog.text
    assert collector.stats.clips == 1


@needs_ffmpeg
async def test_silent_sessions_back_off_and_record_nothing(tmp_path: Path, stream: Path) -> None:
    camera = FakeRtspCamera(stream, silent=True)
    await camera.reserve_port()
    await camera.wake(60)
    settings = fast_settings(tmp_path / "d", silence_s=0.5)
    collector = Collector(RtspTarget.parse(camera.url(), "t"), settings)
    stop = asyncio.Event()
    await run_until(collector, stop, lambda: collector.stats.sessions >= 2, timeout=10)
    await camera.sleep()
    assert collector.stats.outcomes.get("silent", 0) >= 2
    assert camera.stats.teardowns == camera.stats.plays  # every silent session closed cleanly
    assert not list((tmp_path / "d" / "clips").glob("*"))
    assert not list((tmp_path / "d" / ".partial").glob("*"))


@needs_ffmpeg
async def test_wrong_password_is_reported_without_the_password(
    tmp_path: Path, stream: Path, caplog: pytest.LogCaptureFixture
) -> None:
    camera = FakeRtspCamera(stream, credentials=("admin", "right"))
    await camera.reserve_port()
    await camera.wake(60)
    target = RtspTarget.parse(camera.url("admin", PASSWORD), "/home/me/.config/vidette/rtsp-url")
    collector = Collector(target, fast_settings(tmp_path / "d"))
    stop = asyncio.Event()
    caplog.set_level(logging.INFO)
    await run_until(collector, stop, lambda: collector.stats.sessions >= 1, timeout=10)
    await camera.sleep()
    assert collector.stats.outcomes == {"unauthorized": 1}
    assert "401" in caplog.text and "rtsp-url" in caplog.text
    assert PASSWORD not in caplog.text


@needs_ffmpeg
async def test_disk_guard_blocks_before_any_connection(tmp_path: Path, stream: Path) -> None:
    camera = FakeRtspCamera(stream)
    await camera.reserve_port()
    await camera.wake(60)
    dataset = tmp_path / "d"
    dataset.mkdir()
    (dataset / "old.bin").write_bytes(b"x" * 2_000_000)
    settings = fast_settings(
        dataset, limits=DiskLimits(max_dataset_bytes=1_000_000, min_free_bytes=1)
    )
    collector = Collector(RtspTarget.parse(camera.url(), "t"), settings)
    stop = asyncio.Event()
    await run_until(collector, stop, lambda: collector.state == "disk_full", timeout=5)
    await camera.sleep()
    assert collector.state == "stopped" and camera.stats.connections == 0


@needs_ffmpeg
async def test_short_wake_periods_are_dropped(tmp_path: Path, stream: Path) -> None:
    camera = FakeRtspCamera(stream)
    await camera.reserve_port()
    await camera.wake(1.0)
    settings = fast_settings(tmp_path / "d", min_seconds=3.0)
    collector = Collector(RtspTarget.parse(camera.url(), "t"), settings)
    stop = asyncio.Event()
    await run_until(collector, stop, lambda: collector.stats.sessions >= 1, timeout=10)
    await camera.sleep()
    assert collector.stats.outcomes.get("recorded") == 1
    assert not list((tmp_path / "d" / "clips").glob("*"))
    assert not list((tmp_path / "d" / ".partial").glob("*"))


async def test_one_collector_per_dataset(tmp_path: Path) -> None:
    target = RtspTarget.parse("rtsp://127.0.0.1:1/x", "t")
    first = Collector(target, fast_settings(tmp_path / "d"), prober=_never)
    stop = asyncio.Event()
    task = asyncio.create_task(first.run(stop))
    await asyncio.sleep(0.3)
    second = Collector(target, fast_settings(tmp_path / "d"), prober=_never)
    with pytest.raises(DatasetLockedError, match="one collector per camera"):
        await second.run(asyncio.Event())
    stop.set()
    await task


async def _never(host: str, port: int) -> bool:
    return False


@needs_ffmpeg
async def test_leftover_partials_are_adopted_or_dropped(tmp_path: Path, stream: Path) -> None:
    camera = FakeRtspCamera(stream)
    await camera.reserve_port()
    await camera.wake(60)
    dataset = tmp_path / "d"
    part = partial_path(dataset, time.time() - 60)
    part.parent.mkdir(parents=True)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
        "-i", camera.url(), "-c", "copy", "-t", "3", "-f", "matroska", str(part),
    )  # fmt: skip
    await proc.wait()
    await camera.sleep()
    (dataset / ".partial" / "123.mkv.part").write_bytes(b"not a video")
    adopted = await adopt_leftovers(dataset, camera="porch", min_seconds=1.0)
    assert len(adopted) == 1 and adopted[0].adopted
    assert not list((dataset / ".partial").glob("*"))


async def test_tcp_probe_sends_nothing_and_sees_a_closed_port(tmp_path: Path) -> None:
    received: list[bytes] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.append(await reader.read(100))
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    assert await tcp_reachable("127.0.0.1", port)
    await asyncio.sleep(0.1)
    server.close()
    await server.wait_closed()
    assert received == [b""]
    assert not await tcp_reachable("127.0.0.1", port)


def test_cli_refuses_a_dataset_inside_a_git_work_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    monkeypatch.setenv("VIDETTE_RTSP_URL", f"rtsp://u:{PASSWORD}@cam/live0")
    assert main(["collect", str(tmp_path / "repo" / "datasets" / "real")]) == 2
    err = capsys.readouterr().err
    assert "git work tree" in err and PASSWORD not in err


def test_cli_check_opens_no_rtsp_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg")
    monkeypatch.setenv("VIDETTE_RTSP_URL", f"rtsp://u:{PASSWORD}@127.0.0.1:9/live0")
    code = main(["collect", str(tmp_path / "d"), "--check", "--min-free", "1B"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "rtsp://***@127.0.0.1:9/live0" in out and PASSWORD not in out
    assert "closed" in out and "next clip may take up to" in out
