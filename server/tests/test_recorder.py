from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from vidette.core.config import CameraConfig, CameraSource, PowerProfile, VidetteConfig
from vidette.recording.recorder import CameraRecorder, RecorderSupervisor
from vidette.recording.segments import SegmentNotice

FFMPEG = shutil.which("ffmpeg")
requires_ffmpeg: pytest.MarkDecorator = pytest.mark.skipif(
    FFMPEG is None, reason="ffmpeg not installed"
)


def _camera(power_profile: PowerProfile = PowerProfile.mains) -> CameraConfig:
    return CameraConfig(
        source=CameraSource(main="rtsp://example/main"), power_profile=power_profile
    )


def _make_source_clip(path: Path) -> None:
    subprocess.run(
        [
            FFMPEG or "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=10",
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(path),
        ],
        check=True,
        timeout=60,
    )


@requires_ffmpeg
async def test_recorder_produces_indexed_segments(tmp_path: Path, media_dir: Path) -> None:
    """The real pipeline: ffmpeg loops a clip → fMP4 segments land → notices arrive."""
    clip = tmp_path / "source.mp4"
    _make_source_clip(clip)

    notices: list[SegmentNotice] = []
    events: list[tuple[str, dict[str, Any]]] = []

    async def on_segment(camera_id: str, notice: SegmentNotice) -> None:
        assert camera_id == "cam"
        notices.append(notice)

    async def on_event(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, payload))

    recorder = CameraRecorder(
        "cam",
        _camera(),
        source_url=str(clip),
        media_dir=media_dir,
        on_segment=on_segment,
        on_event=on_event,
        segment_seconds=2,
        stall_after_s=30.0,
        input_args=("-re", "-stream_loop", "-1"),
    )
    await recorder.start()
    try:
        deadline = asyncio.get_running_loop().time() + 30
        while len(notices) < 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.5)
    finally:
        await recorder.stop()

    assert len(notices) >= 2, f"expected ≥2 segments, got {len(notices)}; events: {events}"
    starts = [notice.start_ts for notice in notices]
    assert starts == sorted(starts)
    for notice in notices:
        assert notice.path.exists()
        assert notice.size_bytes > 0
        assert notice.path.is_relative_to(media_dir / "cam")
        assert notice.end_ts > notice.start_ts
    assert recorder.status().state == "stopped"
    assert recorder.status().last_segment_at is not None


async def test_recorder_drains_final_segment_notices_after_stall_kill(
    media_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffmpeg finalizes the in-flight segment on SIGTERM and prints its CSV line at exit —
    after the recorder left its read loop. That last segment must be indexed immediately
    (field case: a burst-streaming camera lost every burst's final segment)."""
    from vidette.recording.segments import camera_media_dir, segment_hour_dir

    epoch = int(time.time()) - 120
    camera_dir = camera_media_dir(media_dir, "porch")
    hour_dir = segment_hour_dir(camera_dir, epoch)
    hour_dir.mkdir(parents=True, exist_ok=True)
    (hour_dir / f"{epoch}.mp4").write_bytes(b"\x00" * 256)

    class StallThenFinalizeProc:
        """Silent while running; on terminate, prints the finalized-segment CSV line."""

        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_eof()

        def terminate(self) -> None:
            if self.returncode is None:
                self.returncode = 0
                self.stdout.feed_data(f"{epoch}.mp4,0.00,7.50\n".encode())
                self.stdout.feed_eof()

        def kill(self) -> None:
            self.terminate()

        async def wait(self) -> int:
            while self.returncode is None:
                await asyncio.sleep(0.005)
            return self.returncode

    async def fake_exec(*args: object, **kwargs: object) -> StallThenFinalizeProc:
        return StallThenFinalizeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    notices: list[SegmentNotice] = []

    async def on_segment(camera: str, notice: SegmentNotice) -> None:
        notices.append(notice)

    async def on_event(kind: str, payload: dict[str, Any]) -> None:
        return None

    recorder = CameraRecorder(
        "porch",
        _camera(),
        source_url="rtsp://gateway/porch",
        media_dir=media_dir,
        on_segment=on_segment,
        on_event=on_event,
        stall_after_s=0.05,
        read_tick_s=0.02,
        initial_backoff_s=30.0,  # park in backoff after the first stall kill
    )
    await recorder.start()
    for _ in range(400):
        await asyncio.sleep(0.01)
        if notices:
            break
    status = recorder.status()
    await recorder.stop()

    assert len(notices) == 1
    assert notices[0].start_ts == float(epoch)
    assert notices[0].end_ts == epoch + 7.5
    assert status.last_segment_at == epoch + 7.5


async def test_recorder_backs_off_and_reports_when_ffmpeg_dies(
    tmp_path: Path, media_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unit: an immediately-exiting child → backoff state, rate-limited exit events, clean stop."""

    class FakeProc:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_data(b"boom: connection refused\n")
            self.stderr.feed_eof()
            self.stdout.feed_eof()  # EOF right away — the child is dead on arrival

        def terminate(self) -> None:
            self.returncode = 1

        def kill(self) -> None:
            self.returncode = 1

        async def wait(self) -> int:
            self.returncode = 1
            return 1

    spawned = 0

    async def fake_exec(*args: object, **kwargs: object) -> FakeProc:
        nonlocal spawned
        spawned += 1
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    events: list[str] = []

    async def on_segment(camera_id: str, notice: SegmentNotice) -> None:  # pragma: no cover
        raise AssertionError("no segments expected from a dead child")

    async def on_event(kind: str, payload: dict[str, Any]) -> None:
        events.append(kind)

    recorder = CameraRecorder(
        "cam",
        _camera(),
        source_url="rtsp://nowhere/cam",
        media_dir=media_dir,
        on_segment=on_segment,
        on_event=on_event,
        initial_backoff_s=0.05,
    )
    await recorder.start()
    deadline = asyncio.get_running_loop().time() + 5
    while recorder.status().restarts < 2 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
    status = recorder.status()
    await recorder.stop()

    assert spawned >= 2
    assert status.restarts >= 2
    assert "recorder.exited" in events
    assert status.last_error is not None and "exited" in status.last_error
    assert recorder.status().state == "stopped"


class _FakeGateway:
    def __init__(self, skipped: dict[str, str]) -> None:
        self.skipped = skipped

    def restream_url(self, camera_id: str, role: str = "main") -> str:
        return f"rtsp://gw:8554/{camera_id}"


class _FakeDb:
    def __init__(self) -> None:
        self.segments: list[dict[str, Any]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def add_segment(self, **kwargs: Any) -> int:
        self.segments.append(kwargs)
        return len(self.segments)

    async def add_system_event(self, kind: str, payload: dict[str, Any]) -> int:
        self.events.append((kind, payload))
        return len(self.events)


def _supervisor_config(media_dir: Path, tmp_path: Path) -> VidetteConfig:
    return VidetteConfig.model_validate(
        {
            "storage": {"media_dir": str(media_dir), "database": str(tmp_path / "db.sqlite")},
            "cameras": {
                "front-door": {"adapter": "rtsp", "source": {"main": "rtsp://cam/1"}},
                "backyard": {"adapter": "rtsp"},  # no source → gateway skips it
                "paused": {"adapter": "rtsp", "source": {"main": "rtsp://cam/2"},
                           "record": {"mode": "off"}},
            },
        }
    )


async def test_supervisor_skips_gateway_missing_and_off_cameras(
    tmp_path: Path, media_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class IdleProc:
        """Stays 'alive' until terminated so the recorder sits in `recording` state."""

        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_eof()

        def terminate(self) -> None:
            if self.returncode is None:
                self.returncode = 0
                self.stdout.feed_eof()

        def kill(self) -> None:
            self.terminate()

        async def wait(self) -> int:
            while self.returncode is None:  # pragma: no cover - exits via terminate()
                await asyncio.sleep(0.01)
            return self.returncode

    async def fake_exec(*args: object, **kwargs: object) -> IdleProc:
        return IdleProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ffmpeg")

    db = _FakeDb()
    gateway = _FakeGateway(skipped={"backyard": "the rtsp adapter requires 'source.main'"})
    supervisor = RecorderSupervisor(
        _supervisor_config(media_dir, tmp_path), db, gateway, media_dir=media_dir  # type: ignore[arg-type]
    )
    await supervisor.start()
    await asyncio.sleep(0.1)
    status = supervisor.status()
    await supervisor.stop()

    assert status["front-door"].state in ("starting", "recording")
    assert status["backyard"].state == "idle"
    assert status["backyard"].last_error is not None
    assert "gateway" in status["backyard"].last_error
    assert "paused" not in status  # record.mode=off → not supervised at all
    assert any(kind == "recorder.source_unavailable" for kind, _ in db.events)


async def test_supervisor_reports_missing_ffmpeg(
    tmp_path: Path, media_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    db = _FakeDb()
    supervisor = RecorderSupervisor(
        _supervisor_config(media_dir, tmp_path),
        db,  # type: ignore[arg-type]
        _FakeGateway(skipped={}),  # type: ignore[arg-type]
        media_dir=media_dir,
    )
    await supervisor.start()
    status = supervisor.status()
    await supervisor.stop()

    assert all(entry.state == "idle" for entry in status.values())
    assert any(kind == "recorder.ffmpeg_missing" for kind, _ in db.events)
    assert all("ffmpeg" in (entry.last_error or "") for entry in status.values())


class SilentProc:
    """Stays alive, never produces stdout — the shape of a sleeping/stalled camera."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()

    def terminate(self) -> None:
        if self.returncode is None:
            self.returncode = -15
            self.stdout.feed_eof()

    def kill(self) -> None:
        self.terminate()

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0.005)
        return self.returncode


async def _run_silent_camera(
    profile: PowerProfile, media_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[tuple[str, dict[str, Any]]], list[float], Any]:
    """Drive a stall streak against a never-sending camera; returns (events, backoffs, status)."""

    async def fake_exec(*args: object, **kwargs: object) -> SilentProc:
        return SilentProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    events: list[tuple[str, dict[str, Any]]] = []
    backoffs: list[float] = []

    async def on_event(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, payload))

    async def on_segment(camera: str, notice: SegmentNotice) -> None:  # pragma: no cover
        raise AssertionError("a silent camera must never produce segments")

    recorder = CameraRecorder(
        "porch",
        _camera(profile),
        source_url="rtsp://gateway/porch",
        media_dir=media_dir,
        on_segment=on_segment,
        on_event=on_event,
        stall_after_s=0.05,
        read_tick_s=0.02,
        initial_backoff_s=1.0,  # doubling reaches either cap within ~10 cycles
    )
    real_wait = recorder._backoff_wait

    async def spying_wait(seconds: float) -> bool:
        backoffs.append(seconds)
        return await real_wait(min(seconds, 0.001))  # record real values, sleep fast

    monkeypatch.setattr(recorder, "_backoff_wait", spying_wait)

    await recorder.start()
    for _ in range(400):
        await asyncio.sleep(0.01)
        if len(backoffs) >= 12:
            break
    status = recorder.status()
    await recorder.stop()
    return events, backoffs, status


async def test_battery_camera_stall_streak_backs_off_and_rate_limits(
    media_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """power_profile=battery: a camera that answers the connection but never sends data
    (Eufy S3 Pro asleep — field incident) must not be hammered forever: recorder.stalled
    is emitted 1st + every 5th, backoff grows past the crash cap toward the 5-minute
    stall cap, and the status hints at a sleeping camera."""
    events, backoffs, status = await _run_silent_camera(
        PowerProfile.battery, media_dir, monkeypatch
    )

    stalled_events = [p for k, p in events if k == "recorder.stalled"]
    # Rate-limited: streak failures 1, 5, 10 → not one event per cycle.
    assert [p["consecutive_failures"] for p in stalled_events][:3] == [1, 5, 10]
    # Backoff kept doubling past the 60 s crash cap and settled at the 300 s stall cap.
    assert max(backoffs) == 300.0
    assert status.last_error is not None and "sleeping" in status.last_error


async def test_mains_camera_stall_streak_retries_fast(
    media_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """power_profile=mains (the default): nothing needs protecting — the same stall
    streak stays capped at 30 s and never claims the camera is sleeping (field
    regression: a mains Eufy stuck in 5-minute backoffs looked completely dead)."""
    events, backoffs, status = await _run_silent_camera(
        PowerProfile.mains, media_dir, monkeypatch
    )

    assert max(backoffs) == 30.0
    assert status.last_error is not None and "sleeping" not in status.last_error
    # Event rate-limiting is profile-independent (log/webhook flood protection).
    stalled_events = [p for k, p in events if k == "recorder.stalled"]
    assert [p["consecutive_failures"] for p in stalled_events][:3] == [1, 5, 10]
