"""Tier 0 runner tests: scripted decoders for the loop logic, real ffmpeg for the decoder."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any, Literal

import numpy as np
import numpy.typing as npt
import pytest
from conftest import FFMPEG, requires_ffmpeg

from vidette.core.config import CameraConfig, CameraSource, VidetteConfig
from vidette.pipeline.base import BBox, Detection, MotionRegion
from vidette.pipeline.decode import SubstreamDecoder
from vidette.pipeline.runner import CameraPipeline, PipelineSupervisor

H, W = 96, 128


def _frame(fill: int = 0) -> npt.NDArray[np.uint8]:
    return np.full((H, W, 3), fill, dtype=np.uint8)


def _square(x0: int, y0: int, *, size: int = 24) -> npt.NDArray[np.uint8]:
    frame = _frame()
    frame[y0 : y0 + size, x0 : x0 + size, :] = 255
    return frame


def _script() -> list[npt.NDArray[np.uint8]]:
    """5 warmup frames + 1 quiet frame + 2 motion frames (gate defaults: warmup 5)."""
    return [_frame()] * 6 + [_square(32, 24), _square(64, 48)]


def _camera() -> CameraConfig:
    return CameraConfig(source=CameraSource(main="rtsp://cam/main", sub="rtsp://cam/sub"))


async def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition not met in time"
        await asyncio.sleep(0.01)


class ScriptedDecoder:
    """FrameSource double: yields a scripted sequence, then blocks (live) or EOFs (dead)."""

    def __init__(
        self,
        script: list[npt.NDArray[np.uint8]],
        *,
        end: Literal["block", "eof"] = "block",
    ) -> None:
        self._script = script
        self._end = end
        self._stopped = asyncio.Event()

    async def frames(self) -> AsyncGenerator[tuple[float, npt.NDArray[np.uint8]]]:
        for i, frame in enumerate(self._script):
            yield float(i), frame
        if self._end == "block":
            await self._stopped.wait()  # stay "live" so the pipeline does not restart

    async def stop(self) -> None:
        self._stopped.set()

    def stderr_tail(self) -> str:
        return "rtsp://nowhere: connection refused"


async def _null_detector(frame: npt.NDArray[np.uint8]) -> list[Detection]:
    return []


async def _null_sink(
    camera_id: str, ts: float, detections: list[Detection], regions: list[MotionRegion]
) -> None:
    return None


# --- CameraPipeline: motion → detector → sink ---------------------------------------------


async def test_motion_flows_through_detector_to_sink() -> None:
    person = Detection(label="person", confidence=0.9, bbox=BBox(x=0.2, y=0.2, w=0.2, h=0.5))
    detector_frames: list[npt.NDArray[np.uint8]] = []

    async def detector(frame: npt.NDArray[np.uint8]) -> list[Detection]:
        detector_frames.append(frame)
        return [person] if len(detector_frames) == 1 else []

    sink_calls: list[tuple[str, float, list[Detection], int]] = []

    async def sink(
        camera_id: str, ts: float, detections: list[Detection], regions: list[MotionRegion]
    ) -> None:
        assert all(0.0 <= r.bbox.x <= 1.0 and r.score > 0.0 for r in regions)
        sink_calls.append((camera_id, ts, detections, len(regions)))

    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, payload))

    factory_calls: list[dict[str, object]] = []

    def factory(
        source_url: str, *, fps: float, height: int, input_args: tuple[str, ...] = ()
    ) -> ScriptedDecoder:
        factory_calls.append({"url": source_url, "fps": fps, "height": height})
        return ScriptedDecoder(_script())

    pipeline = CameraPipeline(
        "front-door",
        _camera(),
        source_url="rtsp://gw:8554/front-door__sub",
        detector=detector,
        sink=sink,
        emit=emit,
        decoder_factory=factory,
        initial_backoff_s=0.05,
    )
    await pipeline.start()
    try:
        await _wait_for(lambda: len(sink_calls) >= 2)
        assert pipeline.status().state == "running"
    finally:
        await pipeline.stop()

    # Decoder was created from the camera's detect settings (defaults: 5 fps @ 720).
    assert factory_calls == [{"url": "rtsp://gw:8554/front-door__sub", "fps": 5.0, "height": 720}]
    assert len(detector_frames) == 2  # detector ran only on the two motion frames
    first, second = sink_calls[0], sink_calls[1]
    assert first[0] == "front-door"
    assert first[2] == [person] and first[3] >= 1
    assert second[2] == []  # motion tick still delivered when the detector sees nothing

    status = pipeline.status()
    assert status.state == "stopped"
    assert status.frames_total == 8
    assert status.motion_frames == 2
    assert status.detect_calls == 2
    assert status.last_frame_at == 7.0
    assert status.restarts == 0
    assert events == []  # healthy run: no error or stall events


async def test_sink_and_detector_failures_do_not_stop_the_loop() -> None:
    async def detector(frame: npt.NDArray[np.uint8]) -> list[Detection]:
        raise RuntimeError("detector exploded")

    sink_calls: list[list[Detection]] = []

    async def sink(
        camera_id: str, ts: float, detections: list[Detection], regions: list[MotionRegion]
    ) -> None:
        sink_calls.append(detections)
        raise RuntimeError("sink exploded")

    events: list[str] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        events.append(kind)

    def factory(
        source_url: str, *, fps: float, height: int, input_args: tuple[str, ...] = ()
    ) -> ScriptedDecoder:
        return ScriptedDecoder(_script())

    pipeline = CameraPipeline(
        "cam",
        _camera(),
        source_url="rtsp://gw:8554/cam__sub",
        detector=detector,
        sink=sink,
        emit=emit,
        decoder_factory=factory,
        initial_backoff_s=0.05,
    )
    await pipeline.start()
    try:
        await _wait_for(lambda: len(sink_calls) >= 2)  # loop survived both explosions
        assert pipeline.status().state == "running"
    finally:
        await pipeline.stop()

    assert sink_calls == [[], []]  # detector failed → sink still got the motion ticks
    assert "pipeline.error" in events
    status = pipeline.status()
    assert status.state == "stopped"
    assert status.frames_total == 8
    assert status.motion_frames == 2
    assert status.detect_calls == 0
    assert status.last_error is not None and "exploded" in status.last_error


async def test_decoder_eof_backs_off_and_restarts() -> None:
    spawned = 0

    def factory(
        source_url: str, *, fps: float, height: int, input_args: tuple[str, ...] = ()
    ) -> ScriptedDecoder:
        nonlocal spawned
        spawned += 1
        return ScriptedDecoder([], end="eof")  # dead on arrival, like a refused RTSP

    events: list[str] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        events.append(kind)

    pipeline = CameraPipeline(
        "cam",
        _camera(),
        source_url="rtsp://gw:8554/cam__sub",
        detector=_null_detector,
        sink=_null_sink,
        emit=emit,
        decoder_factory=factory,
        initial_backoff_s=0.03,
    )
    await pipeline.start()
    try:
        await _wait_for(lambda: pipeline.status().restarts >= 3)
        await _wait_for(lambda: pipeline.status().state == "backoff")
    finally:
        await pipeline.stop()

    assert spawned >= 3
    status = pipeline.status()
    assert status.state == "stopped"
    assert status.restarts >= 3
    assert status.frames_total == 0
    assert "pipeline.stalled" in events
    assert status.last_error is not None and "EOF" in status.last_error


# --- PipelineSupervisor --------------------------------------------------------------------


class _FakeGateway:
    def restream_url(self, camera_id: str, role: Literal["main", "sub"] = "main") -> str:
        suffix = "__sub" if role == "sub" else ""
        return f"rtsp://gw:8554/{camera_id}{suffix}"


def _supervisor_config() -> VidetteConfig:
    return VidetteConfig.model_validate(
        {
            "cameras": {
                "front-door": {"source": {"main": "rtsp://cam/1", "sub": "rtsp://cam/1s"}},
                "garage": {"source": {"main": "rtsp://cam/2"}},  # main only → warning
                "no-detect": {"source": {"main": "rtsp://cam/3"}, "detect": {"enabled": False}},
                "no-source": {},
            }
        }
    )


async def test_supervisor_prefers_substream_and_warns_on_main_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ffmpeg")
    urls: list[str] = []

    def factory(
        source_url: str, *, fps: float, height: int, input_args: tuple[str, ...] = ()
    ) -> ScriptedDecoder:
        urls.append(source_url)
        return ScriptedDecoder([])  # blocks: pipelines stay running until stopped

    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, payload))

    supervisor = PipelineSupervisor(
        _supervisor_config(),
        _FakeGateway(),
        _null_detector,
        _null_sink,
        emit,
        decoder_factory=factory,
        initial_backoff_s=0.05,
    )
    await supervisor.start()
    await asyncio.sleep(0.05)
    status = supervisor.status()
    await supervisor.stop()

    assert sorted(urls) == ["rtsp://gw:8554/front-door__sub", "rtsp://gw:8554/garage"]
    assert status["front-door"].state == "running"
    assert status["garage"].state == "running"
    assert "no-detect" not in status  # detect.enabled: false → not supervised at all
    assert status["no-source"].state == "idle"
    assert "no source" in (status["no-source"].last_error or "")
    no_sub = [payload for kind, payload in events if kind == "pipeline.no_substream"]
    assert [payload["camera"] for payload in no_sub] == ["garage"]
    assert supervisor.status()["front-door"].state == "stopped"


async def test_supervisor_reports_missing_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    spawned = 0

    def factory(
        source_url: str, *, fps: float, height: int, input_args: tuple[str, ...] = ()
    ) -> ScriptedDecoder:
        nonlocal spawned
        spawned += 1
        return ScriptedDecoder([])

    events: list[str] = []

    async def emit(kind: str, payload: dict[str, Any]) -> None:
        events.append(kind)

    supervisor = PipelineSupervisor(
        _supervisor_config(),
        _FakeGateway(),
        _null_detector,
        _null_sink,
        emit,
        decoder_factory=factory,
    )
    await supervisor.start()
    status = supervisor.status()
    await supervisor.stop()

    assert spawned == 0
    assert "pipeline.ffmpeg_missing" in events
    assert all(entry.state == "idle" for entry in status.values())
    assert "ffmpeg" in (status["front-door"].last_error or "")


# --- SubstreamDecoder against real ffmpeg --------------------------------------------------


def _make_clip(path: Path) -> None:
    subprocess.run(
        [
            FFMPEG or "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=10",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        timeout=60,
    )


@requires_ffmpeg
async def test_substream_decoder_yields_frames_then_eof(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    _make_clip(clip)
    decoder = SubstreamDecoder(str(clip), fps=5, height=180, input_args=("-re",))
    assert (decoder.width, decoder.height) == (320, 180)

    collected: list[tuple[float, npt.NDArray[np.uint8]]] = []

    async def consume() -> None:
        async for ts, frame in decoder.frames():
            collected.append((ts, frame))

    await asyncio.wait_for(consume(), timeout=20)  # returning at all ⇒ real EOF, no hang
    await decoder.stop()  # post-EOF stop is a clean no-op

    assert len(collected) >= 5  # 2 s at fps=5 → ~10; ≥5 is safe across ffmpeg versions
    timestamps = [ts for ts, _ in collected]
    assert timestamps == sorted(timestamps)
    for _, frame in collected:
        assert frame.shape == (180, 320, 3)
        assert frame.dtype == np.uint8
    assert max(int(frame.max()) for _, frame in collected) > 0  # real pixels, not zeros
