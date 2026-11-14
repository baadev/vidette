"""One RTSP session with the camera: connect, copy whatever arrives, stop cleanly.

The camera is a battery Eufy over NAS (RTSP). What v0.1 learned the hard way
(tag `nvr-core`, CHANGELOG 0.1.1 to 0.1.3, docs/cameras/eufy.md):

- **one client only**: a second client gets `404 Stream Not Found` on DESCRIBE, so the
  collector runs exactly one ffmpeg at a time and a lock file keeps a second collector off
  the same dataset;
- **dead sessions linger**: after an unclean hangup the camera turns everyone away for
  minutes. Every stop here is clean: `q` on ffmpeg's stdin (ffmpeg sends TEARDOWN and
  finishes the file), SIGTERM only if that is ignored for 5 s, SIGKILL last;
- **late codec parameters**: a camera that just woke can take seconds to send SPS and a
  key frame; with ffmpeg's default probe window the output "has no dimensions" and the
  muxer dies. The input is probed for up to 10 s, and nothing is lost meanwhile (probed
  packets are written too);
- **sleep ends the stream**: when the camera goes back to sleep it drops the connection
  or stops sending. `-timeout` (socket I/O, here the silence limit) turns the second case
  into a clean end of the recording.

The recording goes to a Matroska `.part` file (readable even if the process dies), video
only: the judge does not use sound, and recording conversations is a legal minefield.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from vidette.collect.target import RtspTarget
from vidette.ffmpeg import binary

logger = logging.getLogger(__name__)

PROBE_US = 10_000_000  # analyzeduration/probesize: wait up to 10 s for SPS + key frame
_GRACEFUL_STOP_S = 5.0
_TICK_S = 1.0
_STDERR_LINES = 12


class Outcome(StrEnum):
    recorded = "recorded"  # media arrived; the .part file holds it
    asleep = "asleep"  # refused or unreachable: the camera is not serving right now
    silent = "silent"  # a session opened but no media came before the silence limit
    busy = "busy"  # 404/453/503 on DESCRIBE: another client, or a dead session lingering
    unauthorized = "unauthorized"  # 401: wrong RTSP username or password
    failed = "failed"  # anything else ffmpeg complained about


@dataclass(frozen=True)
class SessionResult:
    outcome: Outcome
    part: Path
    started_at: float | None  # wall clock (epoch s) of the first recorded media
    ended_at: float
    media_seconds: float
    bytes_written: int
    stop_reason: str
    detail: str  # scrubbed tail of ffmpeg's stderr (never contains the URL)
    clean_stop: bool = True  # False if ffmpeg had to be killed (the camera may hold a ghost)


def record_command(
    ffmpeg: str,
    url: str,
    part: Path,
    *,
    max_seconds: float,
    silence_s: float,
    max_bytes: int,
    wallclock: bool = False,
) -> list[str]:
    return [
        ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostats",
        "-progress", "pipe:1",
        "-rtsp_transport", "tcp",
        "-timeout", str(int(silence_s * 1_000_000)),
        "-allowed_media_types", "video",
        "-analyzeduration", str(PROBE_US), "-probesize", str(PROBE_US),
        *wallclock_input(wallclock),
        "-i", url,
        "-map", "0:v:0", "-c", "copy",
        "-t", f"{max_seconds:g}",
        "-fs", str(max_bytes),
        "-f", "matroska", "-y", str(part),
    ]  # fmt: skip


def wallclock_input(enabled: bool) -> list[str]:
    """Stamp each packet with its arrival time instead of the camera's RTP clock.

    For a restreamer such as go2rtc: when the camera drops and comes back while a reader
    is attached, go2rtc splices the new camera session into the same RTSP session, RTP
    timestamps and all, so the clock jumps by up to hours (forward: `-t` ends the clip
    at once and the segment muxer cuts at every key frame; backward: timestamps stall).
    Arrival time stays monotonic and the gap is the real one. Safe for cameras without
    B-frames (Eufy sends none); the cost is the network's jitter in the frame times.
    """
    return ["-use_wallclock_as_timestamps", "1"] if enabled else []


_CONNECT_FAILURES = (
    "Connection refused",
    "No route to host",
    "Host is down",
    "Network is unreachable",
    "Connection to tcp://",
    "Name or service not known",
    "nodename nor servname",
)
_BUSY = ("Stream Not Found", "404", "453", "503", "Not Enough Bandwidth", "Service Unavailable")
_SILENT = ("Failed reading RTSP data", "Could not find codec parameters", "dimensions not set")


def classify(stderr: str, *, media_seconds: float, bytes_written: int) -> Outcome:
    """What a finished session means, from ffmpeg's own words."""
    if media_seconds > 0 and bytes_written > 0:
        return Outcome.recorded
    if "401" in stderr and "Unauthorized" in stderr:
        return Outcome.unauthorized
    if any(marker in stderr for marker in _BUSY) and "DESCRIBE" in stderr:
        return Outcome.busy
    if any(marker in stderr for marker in _CONNECT_FAILURES):
        return Outcome.asleep
    if any(marker in stderr for marker in _SILENT):
        return Outcome.silent
    if any(marker in stderr for marker in _BUSY):
        return Outcome.busy
    return Outcome.failed


def _number(block: dict[str, str], key: str) -> int:
    """A progress counter; ffmpeg prints N/A before the first packet."""
    try:
        return max(0, int(block.get(key, "0")))
    except ValueError:
        return 0


@dataclass
class _Progress:
    media_seconds: float = 0.0
    bytes_written: int = 0
    media_started_wall: float | None = None
    last_advance_mono: float = 0.0


async def record_session(
    target: RtspTarget,
    part: Path,
    *,
    max_seconds: float,
    silence_s: float,
    max_bytes: int,
    stop: asyncio.Event,
    disk_check: Callable[[int], str | None],
    clock: Callable[[], float] = time.time,
    wallclock: bool = False,
) -> SessionResult:
    """Connect once and record until the stream ends, a limit hits or `stop` is set.

    `disk_check(bytes_written)` is asked about once a second; a returned reason stops
    the recording cleanly (the clip so far is kept).
    """
    part.parent.mkdir(parents=True, exist_ok=True)
    command = record_command(
        binary("ffmpeg"),
        target.url,
        part,
        max_seconds=max_seconds,
        silence_s=silence_s,
        max_bytes=max_bytes,
        wallclock=wallclock,
    )
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None and proc.stderr is not None and proc.stdin is not None
    stderr_tail: deque[str] = deque(maxlen=_STDERR_LINES)

    async def drain_stderr() -> None:
        assert proc.stderr is not None
        while line := await proc.stderr.readline():
            stderr_tail.append(line.decode("utf-8", errors="replace").rstrip())

    stderr_task = asyncio.create_task(drain_stderr())
    progress = _Progress(last_advance_mono=time.monotonic())
    block: dict[str, str] = {}
    stop_reason: str | None = None
    # Silence is ffmpeg's to detect (-timeout); this watchdog only catches an ffmpeg that
    # is stuck somewhere -timeout does not reach.
    stuck_after = silence_s + PROBE_US / 1_000_000 + 5.0

    while True:
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=_TICK_S)
        except TimeoutError:
            line = None
        if line == b"":
            break  # stdout closed: ffmpeg is exiting
        if line:
            key, _, value = line.decode("utf-8", errors="replace").strip().partition("=")
            block[key] = value
            if key != "progress":
                continue
            out_us, size = _number(block, "out_time_us"), _number(block, "total_size")
            if out_us / 1e6 > progress.media_seconds or size > progress.bytes_written:
                progress.last_advance_mono = time.monotonic()
            if out_us > 0 and progress.media_started_wall is None:
                progress.media_started_wall = clock() - out_us / 1e6
            progress.media_seconds = max(progress.media_seconds, out_us / 1e6)
            progress.bytes_written = max(progress.bytes_written, size)
            block = {}
            if value == "end":
                continue
        if stop_reason is not None:
            continue  # already asked ffmpeg to finish; keep reading until it does
        if stop.is_set():
            stop_reason = "stopped by the collector"
        elif (reason := disk_check(progress.bytes_written)) is not None:
            stop_reason = f"disk guard: {reason}"
        elif time.monotonic() - progress.last_advance_mono > stuck_after:
            stop_reason = f"no data for {stuck_after:.0f} s"
        if stop_reason is not None:
            await ask_to_finish(proc)

    clean = await wait_or_force(proc)
    with contextlib.suppress(Exception):
        await stderr_task
    stderr = target.scrub("\n".join(stderr_tail))
    bytes_written = part.stat().st_size if part.is_file() else 0
    outcome = classify(
        stderr, media_seconds=progress.media_seconds, bytes_written=bytes_written
    )
    if stop_reason is None:
        if outcome is not Outcome.recorded:
            stop_reason = outcome.value
        elif progress.media_seconds >= max_seconds - 0.5:
            stop_reason = "max duration"
        elif progress.bytes_written >= max_bytes * 0.98:
            stop_reason = "per-clip size cap (disk guard)"
        else:
            stop_reason = "stream ended (camera asleep or silent)"
    return SessionResult(
        outcome=outcome,
        part=part,
        started_at=progress.media_started_wall,
        ended_at=clock(),
        media_seconds=progress.media_seconds,
        bytes_written=bytes_written,
        stop_reason=stop_reason,
        detail=" | ".join(line for line in stderr.splitlines() if line.strip())[-400:],
        clean_stop=clean,
    )


async def ask_to_finish(proc: asyncio.subprocess.Process) -> None:
    """`q` is ffmpeg's own clean exit: TEARDOWN to the camera, trailer to the file."""
    if proc.stdin is None or proc.returncode is not None:
        return
    with contextlib.suppress(OSError, RuntimeError):
        proc.stdin.write(b"q")
        await proc.stdin.drain()
        proc.stdin.close()


async def wait_or_force(proc: asyncio.subprocess.Process) -> bool:
    """Wait for exit; escalate q → SIGTERM → SIGKILL. True when it ended without a kill."""
    try:
        await asyncio.wait_for(proc.wait(), timeout=_GRACEFUL_STOP_S)
        return True
    except TimeoutError:
        pass
    await ask_to_finish(proc)
    with contextlib.suppress(ProcessLookupError):
        proc.send_signal(signal.SIGTERM)  # ffmpeg treats it like q: finish and exit
    try:
        await asyncio.wait_for(proc.wait(), timeout=_GRACEFUL_STOP_S)
        return True
    except TimeoutError:
        pass
    logger.warning(
        "ffmpeg ignored q and SIGTERM; killing it. The camera may now hold a dead session "
        "for a few minutes (it will answer 404 until it times out)"
    )
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    await proc.wait()
    return False
