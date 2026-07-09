"""Camera recorder + supervisor.

Principle 2 applies literally here: recording is sacred. The recorder is crash-only — any
exception in indexing/notification paths is contained and reported; the ffmpeg child is
restarted with exponential backoff (1→2→4→…→60 s, ±20 % jitter); repeated failures surface
as system events, never as silent death.

Watchdog: if a running recorder produces no finalized segment for `stall_after_s` (default
45 s ≈ 4 missed segments), a "recorder.stalled" system event is emitted and ffmpeg restarts.

Source: always the go2rtc restream (`Go2rtcManager.restream_url`) — one connection per
camera (ADR-0002). The *main* stream role is recorded.

ffmpeg 8.1 field note (see segments.py): the segment muxer does not create strftime
directories, so the recorder pre-creates the current and next hour directories and refreshes
them on every read tick — an hour rollover therefore never kills a recording.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import shutil
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from vidette.core.config import CameraConfig, RecordMode, VidetteConfig
from vidette.db import Database
from vidette.recording.segments import (
    SegmentNotice,
    build_record_command,
    camera_media_dir,
    parse_segment_list_line,
    segment_hour_dir,
)
from vidette.streams.go2rtc import Go2rtcManager

logger = logging.getLogger(__name__)

RecorderState = Literal["idle", "starting", "recording", "backoff", "stopped"]

SystemEventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]

_BACKOFF_MAX_S = 60.0
# Stalls back off much further than crashes: a stall streak usually means a battery camera
# is asleep (field case: Eufy S3 Pro over NAS-RTSP). Hammering it every ~45 s kept the
# camera awake, drained its battery, and leaked one orphaned gateway session per attempt.
_STALL_BACKOFF_MAX_S = 300.0
_READ_TICK_S = 5.0
_STDERR_TAIL_LINES = 20
_EXIT_EVENT_EVERY = 5  # emit recorder.exited/.stalled on the 1st failure, then every 5th
_SLEEPY_HINT = (
    "if this is a battery camera it is probably sleeping — retries continue with growing "
    "backoff (up to 5 min); recording resumes automatically when the camera answers"
)


@dataclass
class CameraRecorderStatus:
    camera: str
    state: RecorderState
    last_segment_at: float | None
    last_error: str | None
    restarts: int


class CameraRecorder:
    def __init__(
        self,
        camera_id: str,
        camera: CameraConfig,
        *,
        source_url: str,
        media_dir: Path,
        on_segment: Callable[[str, SegmentNotice], Awaitable[None]],
        on_event: SystemEventCallback,
        segment_seconds: int = 10,
        stall_after_s: float = 45.0,
        input_args: tuple[str, ...] = ("-rtsp_transport", "tcp"),
        initial_backoff_s: float = 1.0,  # test seam: keeps failure tests fast
        read_tick_s: float = _READ_TICK_S,  # test seam: stall tests need sub-second ticks
    ) -> None:
        self.camera_id = camera_id
        self.camera = camera
        self._source_url = source_url
        self._camera_dir = camera_media_dir(media_dir, camera_id)
        self._on_segment = on_segment
        self._on_event = on_event
        self._segment_seconds = segment_seconds
        self._stall_after_s = stall_after_s
        self._input_args = input_args
        self._initial_backoff_s = initial_backoff_s
        self._read_tick_s = read_tick_s

        self._state: RecorderState = "idle"
        self._last_segment_at: float | None = None
        self._last_error: str | None = None
        self._restarts = 0
        self._task: asyncio.Task[None] | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._stopping = asyncio.Event()

    def status(self) -> CameraRecorderStatus:
        return CameraRecorderStatus(
            camera=self.camera_id,
            state=self._state,
            last_segment_at=self._last_segment_at,
            last_error=self._last_error,
            restarts=self._restarts,
        )

    async def start(self) -> None:
        """Spawn the supervise loop as a task; idempotent."""
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(
                self._supervise(), name=f"vidette-recorder-{self.camera_id}"
            )

    async def stop(self) -> None:
        """Terminate ffmpeg gracefully (SIGTERM, then kill after 5 s); await loop exit."""
        self._stopping.set()
        await self._terminate_proc()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._state = "stopped"

    # --- internals ------------------------------------------------------------------------

    async def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        """Event emission must never take the recorder down."""
        try:
            await self._on_event(kind, payload)
        except Exception:
            logger.exception("recorder %s: event callback failed for %s", self.camera_id, kind)

    def _ensure_hour_dirs(self) -> None:
        now = time.time()
        for epoch in (now, now + 3600):
            segment_hour_dir(self._camera_dir, epoch).mkdir(parents=True, exist_ok=True)

    async def _terminate_proc(self) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()

    async def _backoff_wait(self, seconds: float) -> bool:
        """Interruptible jittered sleep; returns True when stop was requested."""
        jittered = seconds * random.uniform(0.8, 1.2)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=jittered)
        return self._stopping.is_set()

    async def _drain_stderr(
        self, proc: asyncio.subprocess.Process, sink: deque[str]
    ) -> None:
        if proc.stderr is None:
            return
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            sink.append(line.decode("utf-8", errors="replace").rstrip())

    async def _supervise(self) -> None:
        backoff = self._initial_backoff_s
        consecutive_failures = 0
        while not self._stopping.is_set():
            self._state = "starting"
            try:
                self._ensure_hour_dirs()
            except OSError as exc:
                self._last_error = f"cannot create media directories: {exc}"
                await self._emit(
                    "recorder.media_dir_failed",
                    {"camera": self.camera_id, "error": str(exc)},
                )
                self._state = "backoff"
                if await self._backoff_wait(backoff):
                    break
                backoff = min(_BACKOFF_MAX_S, backoff * 2)
                continue

            command = build_record_command(
                self._source_url,
                self._camera_dir,
                self._segment_seconds,
                input_args=self._input_args,
            )
            try:
                proc = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                self._last_error = f"cannot start ffmpeg: {exc}"
                await self._emit(
                    "recorder.spawn_failed", {"camera": self.camera_id, "error": str(exc)}
                )
                self._state = "backoff"
                if await self._backoff_wait(backoff):
                    break
                backoff = min(_BACKOFF_MAX_S, backoff * 2)
                continue

            self._proc = proc
            self._state = "recording"
            stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
            stderr_task = asyncio.create_task(self._drain_stderr(proc, stderr_tail))
            stalled = False
            last_activity = time.monotonic()

            assert proc.stdout is not None  # PIPE requested above
            while True:
                try:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=self._read_tick_s
                    )
                except TimeoutError:
                    if self._stopping.is_set():
                        break
                    with contextlib.suppress(OSError):
                        self._ensure_hour_dirs()  # survive hour rollovers
                    if time.monotonic() - last_activity > self._stall_after_s:
                        stalled = True
                        # Rate-limited like recorder.exited — a sleeping battery camera
                        # stalls every cycle and must not flood the event log/webhooks.
                        if (
                            consecutive_failures == 0
                            or (consecutive_failures + 1) % _EXIT_EVENT_EVERY == 0
                        ):
                            await self._emit(
                                "recorder.stalled",
                                {
                                    "camera": self.camera_id,
                                    "stall_after_s": self._stall_after_s,
                                    "consecutive_failures": consecutive_failures + 1,
                                },
                            )
                        break
                    continue
                if not line:
                    break  # EOF — the process is exiting
                notice = parse_segment_list_line(
                    line.decode("utf-8", errors="replace"), self._camera_dir
                )
                if notice is None:
                    continue
                last_activity = time.monotonic()
                self._last_segment_at = notice.end_ts
                consecutive_failures = 0
                backoff = self._initial_backoff_s
                try:
                    await self._on_segment(self.camera_id, notice)
                except Exception as exc:
                    logger.exception(
                        "recorder %s: indexing failed for %s", self.camera_id, notice.path
                    )
                    await self._emit(
                        "recorder.index_failed",
                        {
                            "camera": self.camera_id,
                            "path": str(notice.path),
                            "error": str(exc),
                        },
                    )

            await self._terminate_proc()
            with contextlib.suppress(Exception):
                await stderr_task
            returncode = proc.returncode
            self._proc = None
            if self._stopping.is_set():
                break

            consecutive_failures += 1
            self._restarts += 1
            tail = " | ".join(stderr_tail)
            reason = (
                f"stalled (no finalized segment for {self._stall_after_s:.0f} s)"
                if stalled
                else f"ffmpeg exited with code {returncode}"
            )
            if stalled and consecutive_failures >= 2:
                reason += f" — {_SLEEPY_HINT}"
            self._last_error = reason + (f" — {tail[-300:]}" if tail else "")
            if consecutive_failures == 1 or consecutive_failures % _EXIT_EVENT_EVERY == 0:
                await self._emit(
                    "recorder.exited",
                    {
                        "camera": self.camera_id,
                        "code": returncode,
                        "stalled": stalled,
                        "consecutive_failures": consecutive_failures,
                        "stderr_tail": tail[-500:],
                    },
                )
            self._state = "backoff"
            if await self._backoff_wait(backoff):
                break
            cap = _STALL_BACKOFF_MAX_S if stalled else _BACKOFF_MAX_S
            backoff = min(cap, backoff * 2)

        self._state = "stopped"


class RecorderSupervisor:
    """Owns one CameraRecorder per camera with record.mode != off.

    M1 note (documented in configuration.md): record modes `motion`/`events` fall back to
    continuous until M2 ships detection — recording *more* than asked, never less.
    """

    def __init__(
        self,
        config: VidetteConfig,
        db: Database,
        gateway: Go2rtcManager,
        *,
        media_dir: Path,
    ) -> None:
        self._config = config
        self._db = db
        self._gateway = gateway
        self._media_dir = media_dir
        self._recorders: dict[str, CameraRecorder] = {}
        self._unavailable: dict[str, str] = {}  # camera id → why it is not recording
        self._started = False

    async def _index_segment(self, camera_id: str, notice: SegmentNotice) -> None:
        await self._db.add_segment(
            camera=camera_id,
            start_ts=notice.start_ts,
            end_ts=notice.end_ts,
            path=str(notice.path),
            size_bytes=notice.size_bytes,
            klass="continuous",
        )

    async def _record_event(self, kind: str, payload: dict[str, Any]) -> None:
        try:
            await self._db.add_system_event(kind, payload)
        except Exception:
            logger.exception("failed to persist system event %s", kind)

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        wanted = {
            camera_id: camera
            for camera_id, camera in self._config.cameras.items()
            if camera.record.mode is not RecordMode.off
        }
        if not wanted:
            return
        if shutil.which("ffmpeg") is None:
            reason = (
                "ffmpeg not found — install ffmpeg (it ships in the official container); "
                "recording is disabled until then"
            )
            self._unavailable = dict.fromkeys(sorted(wanted), reason)
            await self._record_event(
                "recorder.ffmpeg_missing", {"cameras": sorted(wanted), "detail": reason}
            )
            return
        for camera_id, camera in wanted.items():
            skip_reason = self._gateway.skipped.get(camera_id)
            if skip_reason is not None:
                self._unavailable[camera_id] = f"no stream at the gateway: {skip_reason}"
                await self._record_event(
                    "recorder.source_unavailable",
                    {"camera": camera_id, "detail": skip_reason},
                )
                continue
            recorder = CameraRecorder(
                camera_id,
                camera,
                source_url=self._gateway.restream_url(camera_id, "main"),
                media_dir=self._media_dir,
                on_segment=self._index_segment,
                on_event=self._record_event,
            )
            self._recorders[camera_id] = recorder
            await recorder.start()

    async def stop(self) -> None:
        await asyncio.gather(
            *(recorder.stop() for recorder in self._recorders.values()),
            return_exceptions=True,
        )
        self._started = False

    def status(self) -> dict[str, CameraRecorderStatus]:
        statuses = {camera_id: rec.status() for camera_id, rec in self._recorders.items()}
        for camera_id, reason in self._unavailable.items():
            statuses[camera_id] = CameraRecorderStatus(
                camera=camera_id,
                state="idle",
                last_segment_at=None,
                last_error=reason,
                restarts=0,
            )
        return statuses
