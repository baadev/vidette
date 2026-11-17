"""The collector loop: wait gently for the camera, record each wake period, repeat.

    asleep ──(TCP port opens)──▶ connect ──(media)──▶ record ──(stream ends)──▶ finalize
       ▲          every 5 s          │                                           │
       └──────────────────────────────┴── backoff on 404 / silence / errors ◀──────┘

Being gentle with a battery camera (lessons from v0.1, tag `nvr-core`):

- **No RTSP while it sleeps.** The wait is a bare TCP connect to the RTSP port every
  `probe_every` (default 5 s), closed at once: no DESCRIBE, so no session the camera has
  to keep or time out. Only an open port gets a real session.
- **Back off when it says no.** `404 Stream Not Found` (another client, or a dead session
  the camera still holds) backs off 30 s, doubling to 5 min. A session that opens but
  never sends media backs off 15 s, doubling to 5 min: v0.1 showed that reconnecting every
  45 s kept a sleeping battery camera awake and drained it. Wrong credentials retry every 10 min.
- **One client.** One ffmpeg at a time, and a lock file keeps a second collector off the
  same dataset. Do not point VLC or the Home Assistant RTSP integration at the camera
  while collecting.
- **A pause after each clip** (`cooldown`, 10 s) before the next probe, so the camera
  can finish going back to sleep.

Watched time (excluding time blocked by the disk guard) is kept in
`.vidette/collect-state.json`; `vidette label` uses it as the dataset's observed hours.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from vidette.collect.clips import (
    ClipMeta,
    adopt_leftovers,
    finalize,
    partial_path,
    reserve_id,
    write_early,
)
from vidette.collect.disk import DiskLimits, DiskState, measure
from vidette.collect.prune import Pruner, PruneSettings
from vidette.collect.session import CLUSTER_MS, Outcome, SessionResult, record_session
from vidette.collect.target import RtspTarget
from vidette.incident.mode import IncidentMode, IncidentWatch
from vidette.units import format_duration, format_size

logger = logging.getLogger(__name__)

STATE_DIR = ".vidette"
STATE_FILE = "collect-state.json"
LOCK_FILE = "collect.lock"
_STATE_SAVE_EVERY_S = 60.0
_REPEAT_LOG_EVERY = 5  # streaks are logged on the 1st occurrence, then every 5th
INCIDENT_MIN_S = 0.3  # during an incident a recording this long is still kept


@dataclass(frozen=True)
class CollectSettings:
    dataset: Path
    camera: str = "camera"
    min_seconds: float = 3.0
    max_seconds: float = 120.0
    probe_every_s: float = 5.0
    cooldown_s: float = 10.0
    silence_s: float = 8.0
    busy_backoff_s: tuple[float, float] = (30.0, 300.0)
    silent_backoff_s: tuple[float, float] = (15.0, 300.0)
    auth_retry_s: float = 600.0
    disk_recheck_s: float = 60.0
    limits: DiskLimits = field(default_factory=DiskLimits)
    tcp_probe: bool = True  # False: skip the port check and go straight to RTSP
    once: bool = False  # stop after the first clip (setup checks, tests)
    jitter: float = 0.2
    rotation: int = 0  # clockwise degrees: key frames upright, MP4 tagged (pixels untouched)
    # The URL is a base station's (Eufy HomeBase): its port is always open and it answers
    # 404 for a camera that sleeps, so 404 means asleep, not busy (no TCP check, no backoff).
    base_station: bool = False
    # Stamp packets on arrival instead of the camera's guarded clock (session.timestamp_input).
    # Not needed behind a restreamer any more: the jump guard handles its splices.
    wallclock_timestamps: bool = False
    # Write the first `early_s` seconds of each recording to .early/ while it goes on, so
    # an alert need not wait for the clip (0: off). See clips.write_early.
    early_s: float = 0.0
    # `--prune-oldest`: near --max-size, delete the oldest clips nobody needs instead of
    # stopping (collect/prune.py). None: off, the cap stops the collector.
    prune: PruneSettings | None = None
    # A base station's camera woke up (its PIR saw something): that is evidence by itself.
    # Such a recording is kept down to this length even below `min_seconds` (ADR-0022).
    wake_min_seconds: float = 0.5
    # After a woken stream ends at a base station, look again this soon: the camera that
    # just slept may wake again at once (a person still there), and every second of
    # waiting is a second nobody records. Never grows (no backoff while it keeps waking).
    base_reconnect_s: float = 1.0
    # A base station that refuses connections or sends no video (rebooting, RTSP switched
    # off, Wi-Fi drop) is retried at most this far apart: the doubling backoff meant for a
    # single camera left every camera blind for minutes after the base came back.
    base_retry_cap_s: float = 10.0
    # serve's incident-mode file (vidette.incident.mode): while an alert's incident lasts,
    # everything a camera sees is kept. None: no incident mode.
    incident_file: Path | None = None


def backoff(streak: int, schedule: tuple[float, float]) -> float:
    """first, 2×first, 4×first … capped: the n-th consecutive refusal waits this long."""
    first, cap = schedule
    return float(min(cap, first * 2 ** max(0, streak - 1)))


async def tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    """Is anything listening? Connect and close; not a single RTSP byte is sent."""
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (OSError, TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return True


Prober = Callable[[str, int], Awaitable[bool]]


@dataclass
class CollectStats:
    started_at: float = field(default_factory=time.time)
    watched_s: float = 0.0  # carried over from earlier runs, plus this one
    sessions: int = 0
    clips: int = 0
    seconds_recorded: float = 0.0
    bytes_recorded: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)


class DatasetLockedError(Exception):
    pass


class Collector:
    def __init__(
        self,
        target: RtspTarget,
        settings: CollectSettings,
        *,
        prober: Prober | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.target = target
        self.settings = settings
        self._probe = prober or (lambda host, port: tcp_reachable(host, port))
        self._clock = clock
        self.stats = CollectStats()
        self.state = "starting"
        self._streak: dict[Outcome, int] = {}
        self._lock_file: TextIO | None = None
        self._last_save = 0.0
        self._disk_blocked: str | None = None
        self._early_id: str | None = None  # the id the early window of this session took
        self._out_since: float | None = None  # wall time the base station stopped answering
        self._pruner = (
            None
            if settings.prune is None
            else Pruner(settings.dataset, settings.prune, clock=clock)
        )
        self._incident = IncidentWatch(settings.incident_file, clock=clock)
        self._incident_said: str | None = None  # the incident the log announced last

    def incident(self) -> IncidentMode | None:
        """serve's incident mode in force now (said once in the log per incident)."""
        mode = self._incident.active()
        if mode is not None and mode.event_id != self._incident_said:
            self._incident_said = mode.event_id
            logger.info(
                "incident mode until %s (alert %s on %s): every woken stream is kept whole",
                datetime.fromtimestamp(mode.until).strftime("%H:%M:%S"),
                mode.event_id,
                mode.camera,
            )
        return mode

    def min_keep_s(self) -> float:
        """The shortest recording kept: anything during an incident; a base station's
        woken stream down to `wake_min_seconds`; else `min_seconds`."""
        if self.incident() is not None:
            return min(self.settings.min_seconds, INCIDENT_MIN_S)
        if self.settings.base_station:
            return min(self.settings.min_seconds, self.settings.wake_min_seconds)
        return self.settings.min_seconds

    # --- paths, state, lock -------------------------------------------------------------

    @property
    def _state_path(self) -> Path:
        return self.settings.dataset / STATE_DIR / STATE_FILE

    def _load_state(self) -> None:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            self.stats.watched_s = float(data.get("watched_seconds", 0.0))
            self.stats.clips = int(data.get("clips", 0))
        except (OSError, ValueError):
            pass

    def save_state(self) -> None:
        data: dict[str, Any] = {
            "watched_seconds": round(self.stats.watched_s, 1),
            "watched_hours": round(self.stats.watched_s / 3600, 2),
            "clips": self.stats.clips,
            "camera": self.settings.camera,
            "updated_at": datetime.now(UTC).isoformat(),
            "note": "watched = the collector was running and allowed to record (disk guard "
            "not blocking); vidette label uses it as observed_hours",
        }
        path = self._state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        self._last_save = time.monotonic()

    def _acquire_lock(self) -> None:
        path = self.settings.dataset / STATE_DIR / LOCK_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.seek(0)
            holder = handle.read().strip() or "another process"
            handle.close()
            raise DatasetLockedError(
                f"another collector ({holder}) is already writing to {self.settings.dataset}; "
                "a camera serves one RTSP client, so run one collector per camera"
            ) from None
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid {os.getpid()}")
        handle.flush()
        self._lock_file = handle

    def _release_lock(self) -> None:
        if self._lock_file is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None

    # --- helpers --------------------------------------------------------------------------

    async def _wait(self, seconds: float, stop: asyncio.Event, *, watched: bool = True) -> None:
        jittered = seconds * random.uniform(1 - self.settings.jitter, 1 + self.settings.jitter)
        start = time.monotonic()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=max(0.0, jittered))
        if watched:
            self.stats.watched_s += time.monotonic() - start
        if time.monotonic() - self._last_save > _STATE_SAVE_EVERY_S:
            with contextlib.suppress(OSError):
                self.save_state()

    def _log_streak(self, outcome: Outcome, message: str, *args: object) -> None:
        streak = self._streak.get(outcome, 0)
        if streak == 1 or streak % _REPEAT_LOG_EVERY == 0:
            logger.info(self.target.scrub(message), *args)

    async def _prune(
        self, disk: DiskState, *, skip: tuple[str, ...] = (), need_bytes: int = 0
    ) -> DiskState:
        """`--prune-oldest`: when the folder is near its cap, delete its oldest clips that
        nothing needs (in a thread: it walks the folder) and measure again."""
        pruner = self._pruner
        if pruner is None:
            return disk
        if not pruner.wanted(disk, need_bytes):
            pruner.settle()
            return disk
        if not pruner.due():
            return disk
        result = await asyncio.to_thread(pruner.run, disk)
        if not result.pruned:
            return disk
        logger.info("%s", result.line())
        self.stats.clips = max(0, self.stats.clips - result.pruned)
        with contextlib.suppress(OSError):
            self.save_state()
        return measure(self.settings.dataset, self.settings.limits, skip=skip)

    def _with_prune_note(self, blocked: str, disk: DiskState) -> str:
        """The cap's block, with why pruning did not make room (the floor is not the
        cap: pruning never runs for it)."""
        note = self._pruner.note if self._pruner is not None else None
        if note is None or disk.free_bytes < self.settings.limits.min_free_bytes:
            return blocked
        return f"{blocked}; {note}"

    def _disk_check(self, part_bytes: int) -> str | None:
        disk = measure(self.settings.dataset, self.settings.limits)
        if disk.free_bytes < self.settings.limits.min_free_bytes:
            return disk.blocked_reason
        if disk.dataset_bytes > self.settings.limits.max_dataset_bytes:
            return disk.blocked_reason
        return None

    # --- the loop ---------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> CollectStats:
        settings = self.settings
        for sub in ("clips", "frames"):
            (settings.dataset / sub).mkdir(parents=True, exist_ok=True)
        self._acquire_lock()
        try:
            self._load_state()
            for meta in await adopt_leftovers(
                settings.dataset,
                camera=settings.camera,
                min_seconds=settings.min_seconds,
                rotation=settings.rotation,
            ):
                self.stats.clips += 1
                logger.info("adopted %s left by an interrupted run (%s)", meta.clip,
                            format_duration(meta.duration_s))
            logger.info(
                "collecting from %s into %s · clips %s to %s · %s",
                self.target.redacted,
                settings.dataset,
                format_duration(settings.min_seconds),
                format_duration(settings.max_seconds),
                measure(settings.dataset, settings.limits).describe(),
            )
            if self._pruner is not None:
                logger.info("%s", self._pruner.describe(settings.limits.max_dataset_bytes))
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
        """One pass of the loop; True when the collector should exit."""
        settings = self.settings
        disk = await self._prune(measure(settings.dataset, settings.limits))
        blocked = disk.blocked_reason
        if blocked is not None:
            blocked = self._with_prune_note(blocked, disk)
            if blocked != self._disk_blocked:
                logger.warning("not recording: %s; checking again every %s", blocked,
                               format_duration(settings.disk_recheck_s))
            self._disk_blocked = blocked
            self.state = "disk_full"
            await self._wait(settings.disk_recheck_s, stop, watched=False)
            return False
        if self._disk_blocked is not None:
            logger.info("disk guard clear again: %s", disk.describe())
            self._disk_blocked = None

        if settings.tcp_probe and not settings.base_station:
            self.state = "waiting"
            started = time.monotonic()
            reachable = await self._probe(self.target.host, self.target.port)
            self.stats.watched_s += time.monotonic() - started
            if not reachable:
                await self._wait(settings.probe_every_s, stop)
                return False

        self.state = "recording"
        started = time.monotonic()
        part = partial_path(settings.dataset, self._clock())
        self._early_id = None
        early_task: asyncio.Task[None] | None = None
        requested = self._clock()

        def progress(media_s: float, started_wall: float | None) -> None:
            nonlocal early_task
            if (
                settings.early_s > 0
                and early_task is None
                and started_wall is not None
                and media_s >= settings.early_s + CLUSTER_MS / 1000
            ):
                early_task = asyncio.create_task(self._write_early(part, started_wall, requested))

        result = await record_session(
            self.target,
            part,
            max_seconds=settings.max_seconds,
            silence_s=settings.silence_s,
            max_bytes=disk.clip_allowance_bytes,
            stop=stop,
            disk_check=self._disk_check,
            clock=self._clock,
            wallclock=settings.wallclock_timestamps,
            on_progress=progress,
        )
        if early_task is not None:
            with contextlib.suppress(Exception):
                await early_task
        self.stats.watched_s += time.monotonic() - started
        self.stats.sessions += 1
        self.stats.outcomes[result.outcome.value] = (
            self.stats.outcomes.get(result.outcome.value, 0) + 1
        )
        return await self._after(result, stop)

    async def _after(self, result: SessionResult, stop: asyncio.Event) -> bool:
        settings = self.settings
        outcome = result.outcome
        for other in Outcome:
            if other is not outcome:
                self._streak[other] = 0
        self._streak[outcome] = self._streak.get(outcome, 0) + 1
        streak = self._streak[outcome]
        if outcome in (Outcome.recorded, Outcome.busy):
            self._answering_again()

        if outcome is Outcome.recorded:
            meta = await self._finalize(result)
            if settings.once and meta is not None:
                return True
            if settings.base_station:
                # the base station says 404 while the camera sleeps: no rest is needed to
                # be gentle, and a camera that woke again at once must not wait for us
                await self._wait(settings.base_reconnect_s, stop)
                return False
            if not result.clean_stop:
                await self._wait(settings.busy_backoff_s[0], stop)
            await self._wait(settings.cooldown_s, stop)
            return False
        if result.part.exists():
            result.part.unlink(missing_ok=True)
        if outcome is Outcome.asleep:
            await self._wait(settings.probe_every_s, stop)
        elif outcome is Outcome.busy and settings.base_station:
            await self._asleep_at_base(streak, stop)
        else:
            await self._back_off(outcome, streak, result.detail, stop)
        return False

    async def _asleep_at_base(self, streak: int, stop: asyncio.Event) -> None:
        """A base station's 404: the camera sleeps. It answers without waking the camera
        (measured on a Eufy HomeBase: 14 checks in 14 s, all 404, camera stayed asleep), so
        checking every `probe_every` costs no battery and catches a wake within seconds."""
        if streak == 1:
            logger.info(
                "camera asleep (the base station answers 404 until it wakes); checking "
                "every %s",
                format_duration(self.settings.probe_every_s),
            )
        await self._wait(self.settings.probe_every_s, stop)

    async def _back_off(
        self, outcome: Outcome, streak: int, detail: str, stop: asyncio.Event, *,
        watched: bool = True,
    ) -> None:
        """Wait after a session that brought no video, as long as `outcome` deserves."""
        settings = self.settings
        if outcome is Outcome.busy:
            wait = backoff(streak, settings.busy_backoff_s)
            self._log_streak(
                outcome,
                "camera answered 404 (another RTSP client, or a dead session it still holds); "
                "#%d in a row, next try in %s",
                streak,
                format_duration(wait),
            )
        elif outcome is Outcome.silent:
            wait = self._base_wait(outcome, backoff(streak, settings.silent_backoff_s))
            self._log_streak(
                outcome,
                "camera accepted a session but sent no video for %s; #%d in a row, next try "
                "in %s",
                format_duration(settings.silence_s),
                streak,
                format_duration(wait),
            )
        elif outcome is Outcome.unauthorized:
            wait = settings.auth_retry_s
            self._log_streak(
                outcome,
                "camera rejected the username or password (401); fix the URL in %s; "
                "retrying every %s",
                self.target.origin,
                format_duration(wait),
            )
        elif outcome is Outcome.asleep:
            wait = self._base_wait(outcome, backoff(streak, settings.silent_backoff_s))
            self._log_streak(
                outcome,
                "camera unreachable (%s); #%d in a row, next try in %s",
                detail or "connection refused",
                streak,
                format_duration(wait),
            )
        else:
            wait = backoff(streak, settings.silent_backoff_s)
            self._log_streak(outcome, "ffmpeg failed: %s; next try in %s", detail,
                             format_duration(wait))
        await self._wait(wait, stop, watched=watched)

    def _base_wait(self, outcome: Outcome, wait: float) -> float:
        """A base station that stopped answering is retried every few seconds, never minutes."""
        if not self.settings.base_station or outcome not in (Outcome.asleep, Outcome.silent):
            return wait
        if self._out_since is None:
            self._out_since = time.time()
        return min(wait, self.settings.base_retry_cap_s)

    def _answering_again(self) -> None:
        """The base station answers again (video, or a 404 for a sleeping camera)."""
        if self._out_since is None:
            return
        logger.info(
            "base station answers again after %s out",
            format_duration(max(0.0, time.time() - self._out_since)),
        )
        self._out_since = None

    async def _write_early(self, part: Path, started_wall: float, requested: float) -> None:
        """The early window of the recording in progress; never fails the recording."""
        entry_id = reserve_id(self.settings.dataset, started_wall)
        try:
            meta = await write_early(
                part,
                self.settings.dataset,
                entry_id=entry_id,
                camera=self.settings.camera,
                started_at=started_wall,
                window_s=self.settings.early_s,
                rotation=self.settings.rotation,
                timeline={"requested": requested, "first_frame": started_wall},
            )
        except OSError as exc:
            logger.warning("no early window: %s", exc)
            return
        if meta is not None:
            self._early_id = entry_id
            logger.info("early window %s · first %s", entry_id,
                        format_duration(self.settings.early_s))

    async def _finalize(self, result: SessionResult) -> ClipMeta | None:
        started = result.started_at if result.started_at is not None else result.ended_at
        meta = await finalize(
            result.part,
            self.settings.dataset,
            camera=self.settings.camera,
            started_at=started,
            stop_reason=result.stop_reason,
            min_seconds=self.min_keep_s(),
            rotation=self.settings.rotation,
            entry_id=self._early_id,
            timeline=None if result.requested_at is None else {"requested": result.requested_at},
        )
        if meta is None:
            return None
        self.stats.clips += 1
        self.stats.seconds_recorded += meta.duration_s
        self.stats.bytes_recorded += meta.size_bytes
        logger.info(
            "clip %s · %s · %s · %d frames · %s",
            meta.id,
            format_duration(meta.duration_s),
            format_size(meta.size_bytes),
            len(meta.frames),
            meta.stop_reason,
        )
        with contextlib.suppress(OSError):
            self.save_state()
        return meta
