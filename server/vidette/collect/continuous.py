"""`vidette collect --continuous`: a camera that streams all the time, a ring, a motion gate.

    camera ──RTSP──▶ ffmpeg ─┬─ copy ───────────────▶ ring/<time>.mkv  (60 s segments)
                             └─ 4 fps, 256×144 grey, |frame − previous| ──▶ motion gate
    motion event + pre-roll and post-roll ──▶ cut from the ring (copy) ──▶ clips/<id>.mp4

One ffmpeg, one RTSP session: the camera (or its base station) may serve one client per
stream, so the recording and the motion analysis share the connection. The ring is a
lookback buffer, not a recording (ADR-0011): it exists so a clip can start a few seconds
before the motion, and it is trimmed to `--ring-size` and `--ring-keep`.

Disk priority, highest first: the free-space floor (`--min-free`), the clips
(`--max-size`), the ring. The ring never grows into the last `CLIP_RESERVE_BYTES` above
the floor; when clips eat into that, the ring gives up its oldest segments; only when
the ring is down to its last few segments and free space is still below the floor does
recording stop (and it resumes by itself when space comes back).

A clip is cut as soon as the ring holds its end: the open segment is written in
half-second Matroska clusters, flushed at once, so it is read while it grows (it used to
wait up to one `--segment` for the segment to finish). Then it is remuxed to MP4 and gets
its key frames and sidecar exactly like a battery-camera clip, so `vidette label` and the
clip-folder source read it unchanged. With `--early`, a motion event that has run
`early_s` also gets its first seconds (with the pre-roll) in .early/ under the id its clip
will have.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from vidette.collect.clips import (
    CLIPS_DIR,
    FRAMES_DIR,
    MotionInfo,
    adopt_leftovers,
    finalize,
    partial_path,
    reserve_id,
    write_early,
)
from vidette.collect.collector import Collector, CollectSettings, CollectStats, backoff
from vidette.collect.disk import FRAMES_RESERVE_BYTES, GB, MB, free_bytes, measure
from vidette.collect.motion import FrameScorer, MotionDetector, MotionEvent, MotionSettings
from vidette.collect.ring import RING_DIR, Ring, Segment, cut, output_pattern, plan_cut
from vidette.collect.session import (
    CLUSTER_MS,
    PROBE_US,
    Outcome,
    ask_to_finish,
    classify,
    timestamp_input,
    wait_or_force,
)
from vidette.collect.target import RtspTarget
from vidette.ffmpeg import MediaToolError, binary
from vidette.units import format_duration, format_size

logger = logging.getLogger(__name__)

CLIP_RESERVE_BYTES = 512 * MB  # the ring leaves this much above the floor for clips
MIN_RING_SEGMENTS = 3
_TICK_S = 1.0
_STDERR_LINES = 12
_LONG_SESSION_S = 60.0  # a stream that ran this long ended normally; reconnect quickly
_FLOWING_S = 5.0  # analysis frames this recent: the stream is being watched


def default_hwaccel() -> str | None:
    """Hardware decoding for the motion branch: VideoToolbox on a Mac (about 5 % of a
    core for 1080p at 15 fps instead of about 20 %), software elsewhere."""
    return "videotoolbox" if sys.platform == "darwin" else None


@dataclass(frozen=True)
class ContinuousSettings:
    segment_s: float = 60.0
    ring_max_bytes: int = 3 * GB
    ring_max_age_s: float = 24 * 3600.0
    preroll_s: float = 3.0
    postroll_s: float = 3.0
    motion: MotionSettings = field(default_factory=MotionSettings)
    hwaccel: str | None = None
    stall_s: float = 20.0  # no analysis frame for this long: restart the stream
    reconnect_s: tuple[float, float] = (5.0, 300.0)  # after a drop: first wait, cap
    status_every_s: float = 3600.0


def continuous_command(
    ffmpeg: str,
    url: str,
    ring: Path,
    *,
    segment_s: float,
    silence_s: float,
    motion: MotionSettings,
    hwaccel: str | None,
    wallclock: bool = False,
) -> list[str]:
    """One input, two outputs: the ring (stream copy) and the motion branch (raw grey
    difference frames on stdout)."""
    hw = ["-hwaccel", hwaccel] if hwaccel else []
    return [
        ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostats",
        *hw,
        "-rtsp_transport", "tcp",
        "-timeout", str(int(silence_s * 1_000_000)),
        "-allowed_media_types", "video",
        "-analyzeduration", str(PROBE_US), "-probesize", str(PROBE_US),
        *timestamp_input(wallclock),
        "-i", url,
        "-map", "0:v:0", "-c", "copy",
        "-f", "segment", "-segment_time", f"{segment_s:g}", "-segment_format", "matroska",
        # the open segment is readable up to its last half second: early windows and
        # clips are cut from it without waiting for it to finish
        "-segment_format_options", f"flush_packets=1:cluster_time_limit={CLUSTER_MS}",
        "-reset_timestamps", "1", "-strftime", "1", output_pattern(ring),
        "-map", "0:v:0", "-vf", motion.filter_graph(),
        "-f", "rawvideo", "-pix_fmt", "gray", "-flush_packets", "1", "pipe:1",
    ]  # fmt: skip


def clip_window(
    event: MotionEvent, *, preroll_s: float, postroll_s: float, min_s: float, max_s: float
) -> tuple[float, float]:
    """[start, end] of the clip for one motion event, within --min and --max."""
    start = event.start - preroll_s
    end = event.end + postroll_s
    if end - start < min_s:
        end = start + min_s
    return start, min(end, start + max_s)


@dataclass
class PendingClip:
    start: float
    end: float
    event: MotionEvent
    reason: str
    queued_mono: float
    entry_id: str | None = None  # the id its early window took


OPEN_SEGMENT_LAG_S = 2 * CLUSTER_MS / 1000  # the open segment is on disk up to this far back


def readable(segments: list[Segment], now: float) -> list[Segment]:
    """The ring with its open segment counted as finished up to what is on disk (its
    clusters are flushed as they close), so a cut can use it."""
    out: list[Segment] = []
    for segment in segments:
        if segment.complete:
            out.append(segment)
            continue
        written = now - segment.start - OPEN_SEGMENT_LAG_S
        if written > 0:
            out.append(Segment(segment.path, segment.start, written, segment.size_bytes))
    return out


@dataclass(frozen=True)
class StreamResult:
    outcome: Outcome
    seconds: float  # how long the session lasted
    frames: int  # analysis frames received
    stop_reason: str
    detail: str
    clean_stop: bool


@dataclass
class _Counters:
    """Totals since start; the status line reports the difference to the last one."""

    events: int = 0
    clips: int = 0
    dropped: int = 0
    frames: int = 0  # analysis frames (sessions that ended)
    active: int = 0
    light: int = 0


class ContinuousCollector(Collector):
    def __init__(
        self,
        target: RtspTarget,
        settings: CollectSettings,
        continuous: ContinuousSettings,
        *,
        clock: Callable[[], float] = time.time,
        free: Callable[[Path], int] = free_bytes,
    ) -> None:
        super().__init__(target, settings, clock=clock)
        self.cont = continuous
        self.ring = Ring(settings.dataset / RING_DIR)
        self._free = free
        self._pending: list[PendingClip] = []
        self._detector: MotionDetector | None = None
        self._ring_limited_logged = False
        self._clips_blocked: str | None = None
        self._next_status = time.monotonic() + continuous.status_every_s
        self.counters = _Counters()
        self._reported = _Counters()
        self._short_streak = 0
        self._kept_this_run = 0
        self._early_ids: dict[float, str] = {}  # motion start → the id its early window took
        self._early_tasks: set[asyncio.Task[None]] = set()
        max_event = settings.max_seconds - continuous.preroll_s - continuous.postroll_s
        self._max_event_s = max(1.0, max_event)

    # --- the loop ---------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> CollectStats:
        settings = self.settings
        for sub in (CLIPS_DIR, FRAMES_DIR, RING_DIR):
            (settings.dataset / sub).mkdir(parents=True, exist_ok=True)
        self._acquire_lock()
        try:
            self._load_state()
            for meta in await adopt_leftovers(
                settings.dataset,
                camera=settings.camera,
                min_seconds=settings.min_seconds,
                rotation=settings.rotation,
                trigger="motion",
            ):
                self.stats.clips += 1
                logger.info(
                    "adopted %s left by an interrupted run (%s)",
                    meta.clip,
                    format_duration(meta.duration_s),
                )
            segments = await self.ring.scan(writing=False)
            motion = self.cont.motion
            logger.info(
                "collecting continuously from %s into %s · ring %s of %s, at most %s · "
                "clips %s to %s, pre-roll %s, post-roll %s · motion at %g fps, area %.2g%% "
                "to %.2g%%, %d mask(s) · rotate %d° · %s",
                self.target.redacted,
                settings.dataset,
                format_size(sum(s.size_bytes for s in segments)),
                format_size(self.cont.ring_max_bytes),
                format_duration(self.cont.ring_max_age_s),
                format_duration(settings.min_seconds),
                format_duration(settings.max_seconds),
                format_duration(self.cont.preroll_s),
                format_duration(self.cont.postroll_s),
                motion.fps,
                motion.min_area * 100,
                motion.max_area * 100,
                len(motion.masks),
                settings.rotation,
                measure(settings.dataset, settings.limits, skip=(RING_DIR,)).describe(),
            )
            while not stop.is_set():
                if await self._step(stop):
                    break
        finally:
            with contextlib.suppress(OSError):
                self.save_state()
            self._release_lock()
            self.state = "stopped"
        return self.stats

    async def _step(self, stop: asyncio.Event) -> bool:
        settings = self.settings
        floor = await self._floor_reason(writing=False)
        if floor is not None:
            if floor != self._disk_blocked:
                logger.warning(
                    "not recording: %s; checking again every %s",
                    floor,
                    format_duration(settings.disk_recheck_s),
                )
            self._disk_blocked = floor
            self.state = "disk_full"
            await self._wait(settings.disk_recheck_s, stop, watched=False)
            return False
        if self._disk_blocked is not None:
            logger.info(
                "disk guard clear again: %s free", format_size(self._free(settings.dataset))
            )
            self._disk_blocked = None

        self.state = "recording"
        result = await self._stream(stop)
        self.stats.sessions += 1
        self.stats.outcomes[result.outcome.value] = (
            self.stats.outcomes.get(result.outcome.value, 0) + 1
        )
        await self._housekeeping(writing=False)
        if stop.is_set() or (settings.once and self._kept_this_run):
            return True

        outcome = result.outcome
        self.state = "waiting"
        if outcome is Outcome.recorded:
            for other in Outcome:
                self._streak[other] = 0
            if result.seconds >= _LONG_SESSION_S:
                self._short_streak = 0
            else:
                self._short_streak += 1
            wait = backoff(max(1, self._short_streak), self.cont.reconnect_s)
            logger.info(
                "stream ended after %s (%s); reconnecting in %s",
                format_duration(result.seconds),
                result.stop_reason,
                format_duration(wait),
            )
            await self._wait(wait, stop, watched=False)
            return False
        for other in Outcome:
            if other is not outcome:
                self._streak[other] = 0
        self._streak[outcome] = self._streak.get(outcome, 0) + 1
        self._short_streak = 0
        if outcome is Outcome.busy and settings.base_station:
            self.state = "asleep"
            await self._asleep_at_base(self._streak[outcome], stop)
            return False
        await self._back_off(outcome, self._streak[outcome], result.detail, stop, watched=False)
        return False

    # --- one session --------------------------------------------------------------------

    async def _stream(self, stop: asyncio.Event) -> StreamResult:
        settings, cont = self.settings, self.cont
        ring_dir = settings.dataset / RING_DIR
        ring_dir.mkdir(parents=True, exist_ok=True)
        command = continuous_command(
            binary("ffmpeg"),
            self.target.url,
            ring_dir,
            segment_s=cont.segment_s,
            silence_s=settings.silence_s,
            motion=cont.motion,
            hwaccel=cont.hwaccel,
            wallclock=settings.wallclock_timestamps,
        )
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None and proc.stderr is not None
        stderr_tail: deque[str] = deque(maxlen=_STDERR_LINES)

        async def drain_stderr() -> None:
            assert proc.stderr is not None
            while line := await proc.stderr.readline():
                stderr_tail.append(line.decode("utf-8", errors="replace").rstrip())

        scorer = FrameScorer(cont.motion)
        detector = MotionDetector(cont.motion, max_event_s=self._max_event_s)
        self._detector = detector
        frames = 0
        last_frame_mono: float | None = None

        async def read_frames() -> None:
            nonlocal frames, last_frame_mono
            assert proc.stdout is not None
            size = cont.motion.frame_bytes
            while True:
                try:
                    data = await proc.stdout.readexactly(size)
                except (asyncio.IncompleteReadError, ConnectionError):
                    return
                frames += 1
                last_frame_mono = time.monotonic()
                event = detector.feed(scorer.area(data), self._clock())
                if event is not None:
                    self._queue(event, "motion longer than --max" if event.capped else "motion")

        stderr_task = asyncio.create_task(drain_stderr())
        reader = asyncio.create_task(read_frames())
        exited = asyncio.create_task(proc.wait())
        started = last_tick = time.monotonic()
        first_frame_deadline = PROBE_US / 1_000_000 + cont.stall_s
        stop_reason: str | None = None
        try:
            while not exited.done():
                await asyncio.wait({exited}, timeout=_TICK_S)
                now = time.monotonic()
                flowing = last_frame_mono is not None and now - last_frame_mono < _FLOWING_S
                if flowing and self._clips_blocked is None:
                    self.stats.watched_s += now - last_tick
                last_tick = now
                if exited.done():
                    break
                if stop.is_set() or (settings.once and self._kept_this_run):
                    stop_reason = "stopped by the collector"
                elif (floor := await self._floor_reason(writing=True)) is not None:
                    stop_reason = f"disk guard: {floor}"
                elif last_frame_mono is None and now - started > first_frame_deadline:
                    stop_reason = f"no video within {first_frame_deadline:.0f} s"
                elif last_frame_mono is not None and now - last_frame_mono > cont.stall_s:
                    stop_reason = f"no video for {cont.stall_s:.0f} s"
                if stop_reason is not None:
                    break
                self._maybe_early()
                await self._housekeeping(writing=True)
                self._status()
                if now - self._last_save > 60:
                    with contextlib.suppress(OSError):
                        self.save_state()
        finally:
            if stop_reason is not None:
                await ask_to_finish(proc)
            clean = await wait_or_force(proc)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(reader, timeout=5)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(stderr_task, timeout=5)
            exited.cancel()
            self.counters.frames += detector.frames
            self.counters.active += detector.active_frames
            self.counters.light += detector.light_frames
            tail = detector.flush()
            if tail is not None:
                self._queue(tail, "stream ended during motion")
            self._detector = None

        stderr = self.target.scrub("\n".join(stderr_tail))
        outcome = classify(
            stderr,
            media_seconds=frames / cont.motion.fps,
            bytes_written=1 if frames else 0,
        )
        if stop_reason is None:
            stop_reason = "stream ended" if outcome is Outcome.recorded else outcome.value
        return StreamResult(
            outcome=outcome,
            seconds=time.monotonic() - started,
            frames=frames,
            stop_reason=stop_reason,
            detail=" | ".join(line for line in stderr.splitlines() if line.strip())[-400:],
            clean_stop=clean,
        )

    def _queue(self, event: MotionEvent, reason: str) -> None:
        start, end = clip_window(
            event,
            preroll_s=self.cont.preroll_s,
            postroll_s=self.cont.postroll_s,
            min_s=self.settings.min_seconds,
            max_s=self.settings.max_seconds,
        )
        entry_id = self._early_ids.pop(event.start, None)
        self._pending.append(
            PendingClip(start, end, event, reason, time.monotonic(), entry_id)
        )
        self.counters.events += 1

    def _maybe_early(self) -> None:
        """`--early`: once a motion event has run `early_s`, its first seconds (with the
        pre-roll) go to .early/ while it goes on; its clip gets the same id."""
        early_s = self.settings.early_s
        detector = self._detector
        if early_s <= 0 or detector is None or not detector.busy:
            return
        start = detector.open_since
        if start is None or start in self._early_ids:
            return
        if self._clock() - start < early_s + OPEN_SEGMENT_LAG_S:
            return
        self._early_ids[start] = ""  # taken; the id once written
        task = asyncio.create_task(self._write_early_window(start))
        self._early_tasks.add(task)
        task.add_done_callback(self._early_tasks.discard)
        for old in [k for k in self._early_ids if k < start - 3600]:
            del self._early_ids[old]

    async def _write_early_window(self, start: float) -> None:
        settings = self.settings
        try:
            segments = readable(await self.ring.scan(writing=True), self._clock())
            pieces = plan_cut(segments, start - self.cont.preroll_s, start + settings.early_s)
            if not pieces:
                return
            part = partial_path(settings.dataset, start).with_name(f"{int(start * 1000)}-early.mkv")
            entry_id = reserve_id(settings.dataset, pieces[0].wall_start)
            try:
                await cut(pieces, part)
                meta = await write_early(
                    part,
                    settings.dataset,
                    entry_id=entry_id,
                    camera=settings.camera,
                    started_at=pieces[0].wall_start,
                    window_s=pieces[-1].wall_end - pieces[0].wall_start + 1.0,
                    rotation=settings.rotation,
                    timeline={"motion": start, "first_frame": pieces[0].wall_start},
                )
            finally:
                part.unlink(missing_ok=True)
            if meta is not None and start in self._early_ids:
                self._early_ids[start] = entry_id
                logger.info("early window %s · motion since %s", entry_id,
                            datetime.fromtimestamp(start).strftime("%H:%M:%S"))
        except Exception:  # crash containment: an early window never stops the stream
            logger.exception("early window failed; the clip follows as usual")

    # --- disk ---------------------------------------------------------------------------

    async def _floor_reason(self, *, writing: bool) -> str | None:
        """Below the free-space floor even after the ring gave up all it can: why."""
        limits = self.settings.limits
        dataset = self.settings.dataset
        if self._free(dataset) >= limits.min_free_bytes:
            return None
        segments = await self.ring.scan(writing=writing)
        freed = self.ring.trim(
            segments, max_bytes=0, max_age_s=self.cont.ring_max_age_s, now=self._clock(),
            keep_from=self._keep_from(), min_keep=MIN_RING_SEGMENTS,
        )  # fmt: skip
        if freed:
            logger.info(
                "free space below the floor: the ring gave up %d segment(s), %s",
                len(freed),
                format_size(sum(s.size_bytes for s in freed)),
            )
        free = self._free(dataset)
        if free >= limits.min_free_bytes:
            return None
        return (
            f"only {format_size(free)} free on the disk, the floor is "
            f"{format_size(limits.min_free_bytes)} (--min-free)"
        )

    def _keep_from(self) -> float | None:
        """The oldest moment a clip still needs from the ring."""
        needed = [pending.start for pending in self._pending]
        detector = self._detector
        if detector is not None and detector.open_since is not None:
            needed.append(detector.open_since - self.cont.preroll_s)
        return min(needed) - 1.0 if needed else None

    # --- cutting clips and trimming the ring --------------------------------------------

    async def _housekeeping(self, *, writing: bool) -> None:
        """Cut every clip whose video is complete, then trim the ring. Never raises."""
        try:
            segments = await self.ring.scan(writing=writing)
            complete_end = max((s.end for s in segments if s.complete), default=0.0)
            waited_too_long = 2 * self.cont.segment_s + 30
            on_disk = readable(segments, self._clock())
            written_end = max((s.end for s in on_disk), default=0.0)
            for pending in list(self._pending):
                ready = (
                    not writing
                    or complete_end >= pending.end
                    or written_end >= pending.end  # the open segment has it already
                    or time.monotonic() - pending.queued_mono > waited_too_long
                )
                if ready:
                    self._pending.remove(pending)
                    await self._cut(pending, on_disk)
            await self._trim(writing=writing)
        except Exception:  # crash containment: a bad segment never stops the stream
            logger.exception("ring housekeeping failed; continuing")

    async def _trim(self, *, writing: bool) -> None:
        segments = await self.ring.scan(writing=writing)
        ring_bytes = sum(s.size_bytes for s in segments)
        free = self._free(self.settings.dataset)
        room = ring_bytes + free - self.settings.limits.min_free_bytes - CLIP_RESERVE_BYTES
        budget = max(0, min(self.cont.ring_max_bytes, room))
        if budget < self.cont.ring_max_bytes and ring_bytes > budget:
            if not self._ring_limited_logged:
                logger.warning(
                    "free space limits the ring to %s (of %s): it keeps %s above the "
                    "--min-free floor for clips",
                    format_size(budget),
                    format_size(self.cont.ring_max_bytes),
                    format_size(CLIP_RESERVE_BYTES),
                )
                self._ring_limited_logged = True
        elif budget >= self.cont.ring_max_bytes:
            self._ring_limited_logged = False
        self.ring.trim(
            segments,
            max_bytes=budget,
            max_age_s=self.cont.ring_max_age_s,
            now=self._clock(),
            keep_from=self._keep_from(),
            min_keep=MIN_RING_SEGMENTS,
        )

    async def _cut(self, pending: PendingClip, segments: list[Segment]) -> None:
        settings = self.settings
        moment = datetime.fromtimestamp(pending.event.start).strftime("%H:%M:%S")
        pieces = plan_cut(segments, pending.start, pending.end)
        if not pieces:
            self._drop(f"motion at {moment} not kept: the ring holds no video for it")
            return
        disk = measure(settings.dataset, settings.limits, skip=(RING_DIR,))
        need = 2 * sum(piece.estimated_bytes for piece in pieces) + FRAMES_RESERVE_BYTES
        blocked = disk.blocked_reason
        if blocked is None and need > disk.clip_allowance_bytes:
            blocked = f"the clip needs about {format_size(need)}, {disk.describe()}"
        if blocked is not None:
            if blocked != self._clips_blocked:
                logger.warning(
                    "motion at %s not kept: %s; the ring keeps recording", moment, blocked
                )
            self._clips_blocked = blocked
            self.counters.dropped += 1
            return
        if self._clips_blocked is not None:
            logger.info("clips can be kept again: %s", disk.describe())
            self._clips_blocked = None

        part = partial_path(settings.dataset, pending.start)
        try:
            await cut(pieces, part)
        except MediaToolError as exc:
            part.unlink(missing_ok=True)
            self._drop(f"motion at {moment} not kept: {exc}")
            return
        event = pending.event
        meta = await finalize(
            part,
            settings.dataset,
            camera=settings.camera,
            started_at=pieces[0].wall_start,
            ended_at=pieces[-1].wall_end,
            stop_reason=pending.reason,
            min_seconds=settings.min_seconds,
            rotation=settings.rotation,
            trigger="motion",
            motion=MotionInfo(
                active_s=event.active_s,
                peak_area=event.peak_area,
                capped=event.capped,
                preroll_s=self.cont.preroll_s,
                postroll_s=self.cont.postroll_s,
            ),
            timeline={"motion": event.start, "motion_end": event.end},
            entry_id=pending.entry_id or None,
        )
        if meta is None:
            self.counters.dropped += 1
            return
        self.stats.clips += 1
        self.stats.seconds_recorded += meta.duration_s
        self.stats.bytes_recorded += meta.size_bytes
        self.counters.clips += 1
        self._kept_this_run += 1
        logger.info(
            "clip %s · %s · %s · %d frames · motion %s, peak %.1f%% of the frame%s",
            meta.id,
            format_duration(meta.duration_s),
            format_size(meta.size_bytes),
            len(meta.frames),
            format_duration(event.active_s),
            event.peak_area * 100,
            " · capped at --max" if event.capped else "",
        )
        with contextlib.suppress(OSError):
            self.save_state()

    def _drop(self, message: str) -> None:
        self.counters.dropped += 1
        logger.warning("%s", message)

    def gate_totals(self) -> _Counters:
        """Counters including the session still running."""
        now = _Counters(**vars(self.counters))
        detector = self._detector
        if detector is not None:
            now.frames += detector.frames
            now.active += detector.active_frames
            now.light += detector.light_frames
        return now

    def _status(self) -> None:
        if time.monotonic() < self._next_status:
            return
        self._next_status = time.monotonic() + self.cont.status_every_s
        now, last = self.gate_totals(), self._reported
        self._reported = now
        segments = [p for p in (self.settings.dataset / RING_DIR).glob("*.mkv") if p.is_file()]
        ring_bytes = sum(p.stat().st_size for p in segments)
        frames = now.frames - last.frames
        logger.info(
            "status · last %s: %d motion event(s), %d clip(s) kept, %d dropped · motion in "
            "%.1f%% of analysed frames, light changes in %.1f%% · ring %s in %d segment(s) · "
            "%s free",
            format_duration(self.cont.status_every_s),
            now.events - last.events,
            now.clips - last.clips,
            now.dropped - last.dropped,
            100 * (now.active - last.active) / max(1, frames),
            100 * (now.light - last.light) / max(1, frames),
            format_size(ring_bytes),
            len(segments),
            format_size(self._free(self.settings.dataset)),
        )
