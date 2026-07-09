"""Tier 0 substream decoding — one ffmpeg child per camera, raw BGR frames over a pipe.

The decoder asks ffmpeg for rawvideo at a low, fixed fps and reads exact-size frames from
stdout — no container parsing on our side, no cv2 dependency (budget target: Tier 0 stays
under ~2 % of one N100 core per camera, see docs/architecture/ai-pipeline.md).

Raster decision (binding for Tier 0/1): the analysis raster is a fixed 16:9 canvas,
``width = (height * 16 // 9) // 2 * 2``, so the raw byte stream slices deterministically
without probing the source. Non-16:9 sources are stretched to fit; the aspect distortion is
acceptable at Tier 0/1 — motion fractions and normalized boxes survive it — and it keeps
the pipe simple (one read size, known before the first byte arrives).

Process supervision mirrors ``vidette.recording.recorder``: SIGTERM first, SIGKILL after
5 s, stderr drained into a bounded tail for diagnostics.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import AsyncGenerator

import numpy as np
import numpy.typing as npt

Frame = npt.NDArray[np.uint8]
"""HxWx3 uint8 BGR pixel buffer — the currency of Tier 0/1."""

_STDERR_TAIL_LINES = 30
_KILL_AFTER_S = 5.0


def analysis_width(height: int) -> int:
    """Even 16:9 width for the fixed analysis raster (see module docstring)."""
    return (height * 16 // 9) // 2 * 2


class SubstreamDecoder:
    """Decode a (sub)stream into BGR frames at a fixed fps for the motion gate.

    Single-shot: one instance drives one ffmpeg child; create a new decoder to reconnect
    (the pipeline runner does exactly that, with backoff).
    """

    def __init__(
        self,
        source_url: str,
        *,
        fps: float,
        height: int,
        ffmpeg_path: str = "ffmpeg",
        input_args: tuple[str, ...] = ("-rtsp_transport", "tcp"),
    ) -> None:
        if fps <= 0:
            raise ValueError(f"fps must be positive, got {fps}")
        if height <= 0:
            raise ValueError(f"height must be positive, got {height}")
        self.source_url = source_url
        self.fps = fps
        self.height = height
        self.width = analysis_width(height)
        self._ffmpeg_path = ffmpeg_path
        self._input_args = input_args
        self._frame_bytes = self.width * self.height * 3
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)

    def command(self) -> list[str]:
        """The exact ffmpeg invocation — exposed for tests and error reports."""
        return [
            self._ffmpeg_path,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "warning",
            *self._input_args,
            "-i",
            self.source_url,
            "-vf",
            f"fps={self.fps},scale={self.width}:{self.height}",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "pipe:1",
        ]

    def stderr_tail(self) -> str:
        """The last few ffmpeg stderr lines — attach to stall/error events."""
        return "\n".join(self._stderr_tail)

    async def frames(self) -> AsyncGenerator[tuple[float, Frame]]:
        """Yield ``(wall-clock timestamp, HxWx3 uint8 BGR frame)`` until EOF or stop().

        Frames are read-only views over the pipe buffer (numpy ``frombuffer``) — cheap to
        produce; downstream must ``.copy()`` before mutating. The child is always reaped
        when iteration ends, whichever way it ends.
        """
        if self._proc is not None:
            raise RuntimeError("frames() already consumed — create a new SubstreamDecoder")
        proc = await asyncio.create_subprocess_exec(
            *self.command(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=max(2**16, self._frame_bytes),
        )
        self._proc = proc
        assert proc.stdout is not None  # PIPE requested above
        self._stderr_task = asyncio.create_task(self._drain_stderr(proc))
        try:
            while True:
                try:
                    # readexactly loops over partial reads for us and raises at EOF.
                    data = await proc.stdout.readexactly(self._frame_bytes)
                except asyncio.IncompleteReadError:
                    return  # EOF — the child exited or stop() terminated it
                frame = np.frombuffer(data, dtype=np.uint8).reshape(self.height, self.width, 3)
                yield time.time(), frame
        finally:
            await self._shutdown()

    async def stop(self) -> None:
        """Terminate ffmpeg (SIGTERM, then kill after 5 s); idempotent, safe any time."""
        await self._terminate_proc()

    # --- internals ------------------------------------------------------------------------

    async def _shutdown(self) -> None:
        await self._terminate_proc()
        if self._stderr_task is not None:
            with contextlib.suppress(Exception):
                await self._stderr_task
            self._stderr_task = None

    async def _terminate_proc(self) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=_KILL_AFTER_S)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        if proc.stderr is None:
            return
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            self._stderr_tail.append(line.decode("utf-8", errors="replace").rstrip())
