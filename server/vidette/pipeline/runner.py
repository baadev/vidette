"""Tier 0 pipeline runner — decode → motion gate → detector → sink, per camera.

Prime directive 2 applies at one remove: the pipeline never touches the recorder, and
nothing downstream of it (detector, sink, event emission) may kill the decode loop.
Exceptions are contained and reported as rate-limited ``pipeline.error`` events; the
ffmpeg child is restarted with the recorder's backoff discipline (1→2→…→60 s, ±20 %
jitter); repeated EOFs surface as ``pipeline.stalled`` events, never as silent death.

The runner hands *raw* detections plus motion regions to the sink — tracking and event
assembly (Tier 2+) live behind that seam. The sink is called on every motion tick, even
when the detector saw nothing: the tracker side needs the tick to age its tracks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import shutil
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from vidette.core.config import CameraConfig, VidetteConfig
from vidette.pipeline.base import Detection, MotionRegion
from vidette.pipeline.decode import Frame, SubstreamDecoder
from vidette.pipeline.motion import FrameDiffGate

logger = logging.getLogger(__name__)

PipelineState = Literal["idle", "running", "backoff", "stopped"]

DetectorFn = Callable[[Frame], Awaitable[list[Detection]]]
"""Tier 1 seam: full analysis frame in, raw detections out."""

TracksSink = Callable[[str, float, list[Detection], list[MotionRegion]], Awaitable[None]]
"""Downstream seam: (camera_id, ts, detections, regions) — the tracker/event side."""

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]

_BACKOFF_MAX_S = 60.0
_STALL_EVENT_EVERY = 5  # emit pipeline.stalled on the 1st EOF, then every 5th
_ERROR_EVENT_MIN_INTERVAL_S = 30.0  # rate limit for pipeline.error events


class FrameSource(Protocol):
    """What the runner needs from a decoder — SubstreamDecoder or a test double."""

    def frames(self) -> AsyncGenerator[tuple[float, Frame]]: ...

    async def stop(self) -> None: ...

    def stderr_tail(self) -> str: ...


DecoderFactory = Callable[..., FrameSource]


class RestreamGateway(Protocol):
    """The slice of Go2rtcManager the supervisor needs (ADR-0002: one camera connection)."""

    def restream_url(self, camera_id: str, role: Literal["main", "sub"] = "main") -> str: ...


@dataclass
class PipelineStatus:
    camera: str
    state: PipelineState
    frames_total: int
    motion_frames: int
    detect_calls: int
    last_frame_at: float | None
    last_error: str | None
    restarts: int = 0


class CameraPipeline:
    """Tier 0 conductor for one camera: crash-only, recorder-style supervision."""

    def __init__(
        self,
        camera_id: str,
        camera: CameraConfig,
        *,
        source_url: str,
        detector: DetectorFn,
        sink: TracksSink,
        emit: EmitFn,
        input_args: tuple[str, ...] = ("-rtsp_transport", "tcp"),
        decoder_factory: DecoderFactory = SubstreamDecoder,
        initial_backoff_s: float = 1.0,  # test seam: keeps failure tests fast
    ) -> None:
        self.camera_id = camera_id
        self.camera = camera
        self._source_url = source_url
        self._detector = detector
        self._sink = sink
        self._emit = emit
        self._input_args = input_args
        self._decoder_factory = decoder_factory
        self._initial_backoff_s = initial_backoff_s

        self._state: PipelineState = "idle"
        self._frames_total = 0
        self._motion_frames = 0
        self._detect_calls = 0
        self._last_frame_at: float | None = None
        self._last_error: str | None = None
        self._restarts = 0
        self._last_error_emit_at = float("-inf")
        self._task: asyncio.Task[None] | None = None
        self._decoder: FrameSource | None = None
        self._stopping = asyncio.Event()

    def status(self) -> PipelineStatus:
        return PipelineStatus(
            camera=self.camera_id,
            state=self._state,
            frames_total=self._frames_total,
            motion_frames=self._motion_frames,
            detect_calls=self._detect_calls,
            last_frame_at=self._last_frame_at,
            last_error=self._last_error,
            restarts=self._restarts,
        )

    async def start(self) -> None:
        """Spawn the supervise loop as a task; idempotent."""
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(
                self._supervise(), name=f"vidette-pipeline-{self.camera_id}"
            )

    async def stop(self) -> None:
        """Stop the decoder and await loop exit; clean whatever state the loop is in."""
        self._stopping.set()
        decoder = self._decoder
        if decoder is not None:
            with contextlib.suppress(Exception):
                await decoder.stop()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._state = "stopped"

    # --- internals ------------------------------------------------------------------------

    async def _emit_safe(self, kind: str, payload: dict[str, Any]) -> None:
        """Event emission must never take the pipeline down."""
        try:
            await self._emit(kind, payload)
        except Exception:
            logger.exception("pipeline %s: event callback failed for %s", self.camera_id, kind)

    async def _emit_error(self, stage: str, exc: BaseException) -> None:
        """Report a contained failure, rate-limited so a hot loop cannot flood the bus."""
        logger.exception("pipeline %s: %s failed", self.camera_id, stage)
        now = time.monotonic()
        if now - self._last_error_emit_at < _ERROR_EVENT_MIN_INTERVAL_S:
            return
        self._last_error_emit_at = now
        await self._emit_safe(
            "pipeline.error",
            {
                "camera": self.camera_id,
                "stage": stage,
                "error": str(exc) or exc.__class__.__name__,
            },
        )

    async def _backoff_wait(self, seconds: float) -> bool:
        """Interruptible jittered sleep; returns True when stop was requested."""
        jittered = seconds * random.uniform(0.8, 1.2)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=jittered)
        return self._stopping.is_set()

    async def _process_frame(self, ts: float, frame: Frame, gate: FrameDiffGate) -> None:
        """Gate → detector → sink for one frame; downstream failures are contained."""
        regions = gate.process(ts, frame)
        if not regions:
            return
        self._motion_frames += 1
        detections: list[Detection] = []
        try:
            detections = await self._detector(frame)
            self._detect_calls += 1
        except Exception as exc:
            self._last_error = f"detector failed: {exc}"
            await self._emit_error("detector", exc)
        # The sink gets the motion tick even with zero detections (tracker needs it).
        try:
            await self._sink(self.camera_id, ts, detections, regions)
        except Exception as exc:
            self._last_error = f"sink failed: {exc}"
            await self._emit_error("sink", exc)

    async def _supervise(self) -> None:
        backoff = self._initial_backoff_s
        consecutive_failures = 0
        while not self._stopping.is_set():
            gate = FrameDiffGate()  # fresh background per connection: the scene may have moved
            failed = False
            decoder: FrameSource | None = None
            try:
                decoder = self._decoder_factory(
                    self._source_url,
                    fps=self.camera.detect.fps,
                    height=self.camera.detect.resolution,
                    input_args=self._input_args,
                )
            except Exception as exc:
                failed = True
                self._last_error = f"decoder start failed: {exc}"
                await self._emit_error("decoder", exc)

            if decoder is not None:
                self._decoder = decoder
                self._state = "running"
                agen = decoder.frames()
                try:
                    async for ts, frame in agen:
                        if self._stopping.is_set():
                            break
                        consecutive_failures = 0
                        backoff = self._initial_backoff_s
                        self._frames_total += 1
                        self._last_frame_at = ts
                        await self._process_frame(ts, frame, gate)
                except Exception as exc:
                    failed = True
                    self._last_error = str(exc) or exc.__class__.__name__
                    await self._emit_error("decoder", exc)
                finally:
                    with contextlib.suppress(Exception):
                        await agen.aclose()
                    with contextlib.suppress(Exception):
                        await decoder.stop()
                    self._decoder = None

            if self._stopping.is_set():
                break
            consecutive_failures += 1
            self._restarts += 1
            if not failed and (
                consecutive_failures == 1 or consecutive_failures % _STALL_EVENT_EVERY == 0
            ):
                tail = decoder.stderr_tail() if decoder is not None else ""
                self._last_error = "decoder EOF" + (f" — {tail[-300:]}" if tail else "")
                await self._emit_safe(
                    "pipeline.stalled",
                    {
                        "camera": self.camera_id,
                        "consecutive_failures": consecutive_failures,
                        "stderr_tail": tail[-500:],
                    },
                )
            self._state = "backoff"
            if await self._backoff_wait(backoff):
                break
            backoff = min(_BACKOFF_MAX_S, backoff * 2)
        self._state = "stopped"


class PipelineSupervisor:
    """Owns one CameraPipeline per camera with ``detect.enabled`` and a configured source.

    Substream policy: decode ``sub`` when the camera has one; otherwise fall back to
    ``main`` and emit one ``pipeline.no_substream`` warning per such camera at start —
    main-stream decode costs real CPU, and the budget lens wants that visible.

    ffmpeg is checked once at start: if missing, ``pipeline.ffmpeg_missing`` is emitted and
    nothing starts (mirrors the recorder). numpy is an import-time dependency of this
    package — if it were missing this module would not import at all.
    """

    def __init__(
        self,
        config: VidetteConfig,
        gateway: RestreamGateway,
        detector: DetectorFn,
        sink: TracksSink,
        emit: EmitFn,
        *,
        decoder_factory: DecoderFactory = SubstreamDecoder,  # test seam
        initial_backoff_s: float = 1.0,  # test seam
    ) -> None:
        self._config = config
        self._gateway = gateway
        self._detector = detector
        self._sink = sink
        self._emit = emit
        self._decoder_factory = decoder_factory
        self._initial_backoff_s = initial_backoff_s
        self._pipelines: dict[str, CameraPipeline] = {}
        self._unavailable: dict[str, str] = {}  # camera id → why detection is not running
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        wanted: dict[str, tuple[CameraConfig, Literal["main", "sub"]]] = {}
        for camera_id, camera in self._config.cameras.items():
            if not camera.detect.enabled:
                continue
            if camera.source is None:
                self._unavailable[camera_id] = (
                    "no source configured — detection needs a stream to decode; add "
                    f"'cameras.{camera_id}.source' or set detect.enabled: false"
                )
                continue
            wanted[camera_id] = (camera, "sub" if camera.source.sub else "main")
        if not wanted:
            return
        if shutil.which("ffmpeg") is None:
            reason = (
                "ffmpeg not found — install ffmpeg (it ships in the official container); "
                "detection is disabled until then"
            )
            for camera_id in wanted:
                self._unavailable[camera_id] = reason
            await self._emit_safe(
                "pipeline.ffmpeg_missing", {"cameras": sorted(wanted), "detail": reason}
            )
            return
        for camera_id, (camera, role) in wanted.items():
            if role == "main":
                await self._emit_safe(
                    "pipeline.no_substream",
                    {
                        "camera": camera_id,
                        "detail": (
                            "no substream configured — decoding the main stream for "
                            "detection costs significantly more CPU; add 'source.sub' "
                            "if the camera provides one"
                        ),
                    },
                )
            pipeline = CameraPipeline(
                camera_id,
                camera,
                source_url=self._gateway.restream_url(camera_id, role),
                detector=self._detector,
                sink=self._sink,
                emit=self._emit,
                decoder_factory=self._decoder_factory,
                initial_backoff_s=self._initial_backoff_s,
            )
            self._pipelines[camera_id] = pipeline
            await pipeline.start()

    async def stop(self) -> None:
        await asyncio.gather(
            *(pipeline.stop() for pipeline in self._pipelines.values()),
            return_exceptions=True,
        )
        self._started = False

    def status(self) -> dict[str, PipelineStatus]:
        statuses = {camera_id: p.status() for camera_id, p in self._pipelines.items()}
        for camera_id, reason in self._unavailable.items():
            statuses[camera_id] = PipelineStatus(
                camera=camera_id,
                state="idle",
                frames_total=0,
                motion_frames=0,
                detect_calls=0,
                last_frame_at=None,
                last_error=reason,
            )
        return statuses

    async def _emit_safe(self, kind: str, payload: dict[str, Any]) -> None:
        try:
            await self._emit(kind, payload)
        except Exception:
            logger.exception("pipeline supervisor: event callback failed for %s", kind)
