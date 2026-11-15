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
PROGRESS_EVERY_S = 0.25  # how often ffmpeg reports progress (the length limit's grain)
CLUSTER_MS = 500  # Matroska clusters of the part file (it is readable up to the last one)
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
    requested_at: float | None = None  # wall clock when the session was opened


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
        "-progress", "pipe:1", "-stats_period", f"{PROGRESS_EVERY_S:g}",
        "-rtsp_transport", "tcp",
        "-timeout", str(int(silence_s * 1_000_000)),
        "-allowed_media_types", "video",
        "-analyzeduration", str(PROBE_US), "-probesize", str(PROBE_US),
        *timestamp_input(wallclock),
        "-i", url,
        "-map", "0:v:0", "-c", "copy",
        "-fs", str(max_bytes),
        # short clusters, flushed at once: the part file is readable while it grows (the
        # early window is cut from it), and a crash loses at most half a second
        "-flush_packets", "1", "-cluster_time_limit", str(CLUSTER_MS),
        "-f", "matroska", "-y", str(part),
    ]  # fmt: skip


# The camera's own clock, kept monotonic: a packet more than this after the previous one,
# or not after it at all, is a restreamer splicing a reconnect (go2rtc carries the new
# camera session's RTP clock into the same RTSP session, a jump of up to hours either way);
# it continues one frame after the previous packet instead. Real pauses of the camera
# (dropped frames, a short drop-out) stay as they are, up to this long.
JUMP_GUARD_S = 10.0
_GUARD_STEP_S = 1 / 15  # one frame of a typical camera, for the packet after a splice


def jump_guard(max_step_s: float = JUMP_GUARD_S, step_s: float = _GUARD_STEP_S) -> str:
    """The `setts` bitstream filter that keeps the camera's timestamps but repairs jumps.

    Applied to the input (`-bsf:v` before `-i`), so the copy, the segment muxer and the
    decoder of the motion branch all see the repaired clock. The first timed packet keeps
    its time; a packet without one, or after one without one, continues one frame later."""
    delta = "(PTS-PREV_INPTS)"
    usable = (
        f"not(eq(PTS\\,NOPTS))*not(eq(PREV_INPTS\\,NOPTS))*gt({delta}\\,0)"
        f"*lte({delta}\\,{max_step_s:g}/TB)"
    )
    return (
        f"setts=ts=if(eq(PREV_OUTPTS\\,NOPTS)\\,PTS\\,"
        f"PREV_OUTPTS+if({usable}\\,{delta}\\,{step_s:.6f}/TB))"
    )


def timestamp_input(wallclock: bool = False) -> list[str]:
    """Input options for the packets' timestamps.

    Default: the camera's own (RTP) clock, guarded against restreamer splices
    (`jump_guard`). It is the truthful one: measured through go2rtc on a Wi-Fi camera,
    25 s of RTP timestamps were 376 frames 61 to 73 ms apart, while arrival times of the
    same packets came in bursts (47 % less than 30 ms apart) with eight pauses up to
    0.7 s; on a camera with a weak link, arrival pauses reach 13 s. Stamped on arrival,
    such a burst plays as a frozen picture followed by fast-forward, and a judge reads a
    person walking past as a person standing still.

    `wallclock` (`--wallclock-timestamps`) stamps each packet with its arrival time
    instead: monotonic whatever the source does, but delivery jitter becomes the video's
    timeline. Kept for sources whose clock cannot be trusted at all.
    """
    if wallclock:
        return ["-use_wallclock_as_timestamps", "1"]
    return ["-bsf:v", jump_guard()]


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
    on_progress: Callable[[float, float | None], None] | None = None,
) -> SessionResult:
    """Connect once and record until the stream ends, a limit hits or `stop` is set.

    `disk_check(bytes_written)` is asked about once a second; a returned reason stops
    the recording cleanly (the clip so far is kept). `on_progress(media_seconds,
    started_wall)` hears every progress report (about four a second) and must not block.
    """
    requested_at = clock()
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
    # The length limit is ours, not ffmpeg's `-t`: `-t` reads the timestamps before the
    # jump guard repairs them, so a restreamer's splice would end the clip at once. The
    # wall-clock cap only matters when a slow link delivers far less than real time.
    began_mono = time.monotonic()
    wall_cap_s = max_seconds * 3 + 60

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
            if on_progress is not None:
                on_progress(progress.media_seconds, progress.media_started_wall)
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
        elif progress.media_seconds >= max_seconds - PROGRESS_EVERY_S:
            stop_reason = "max duration"
        elif time.monotonic() - began_mono > wall_cap_s:
            stop_reason = f"max duration ({wall_cap_s:.0f} s of wall clock: a slow link)"
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
        requested_at=requested_at,
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
