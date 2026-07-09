"""Janitor: retention enforcement, disk health, housekeeping.

Storage failures are announced as loudly as intruders (principle 2 / storage.md). The
janitor loop (default every 60 s):

1. Retention: load segments from DB → `vidette.recording.retention.plan_deletions` with
   the *global* retention policy (per-camera overrides applied per camera) and, under disk
   pressure, `bytes_to_free` computed to return to `TARGET_FREE_FRACTION`. Delete files
   first, then DB rows; a file already missing is fine (count it), a file that refuses to
   die is a system event.
2. Disk watermarks (shutil.disk_usage on media_dir): free < 15 % → "storage.low" (warn,
   once per crossing, not per tick); free < 10 % → pressure deletions per the planner;
   `unmet_bytes > 0` → "storage.pressure" system event with the numbers.
3. Write probe (every 5th tick, starting with the first): write/fsync/read/delete
   `<media_dir>/.vidette-probe`; failure → "storage.write_failed" system event.
4. Housekeeping: purge expired sessions; `ExportManager.cleanup_old()`.

All state transitions produce system events, deduplicated by (kind, still-true) so the log
is signal, not noise.

Implementation notes:
- The expiry pass runs per camera (effective retention = `camera.record.retention` or the
  global `storage.retention`; segments of cameras no longer configured use the global
  policy so orphans still expire). The pressure pass then runs once, globally, with a
  retention of `forever` for every class — so only pressure deletions fire and the planner
  ordering (oldest continuous, then oldest motion, never event/favorite) is authoritative.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from vidette.core.config import Retention, VidetteConfig
from vidette.db import Database, SegmentRow
from vidette.recording.exporter import ExportManager
from vidette.recording.retention import Segment, SegmentClass, plan_deletions

logger = logging.getLogger(__name__)

WARN_FREE_FRACTION = 0.15
PRESSURE_FREE_FRACTION = 0.10
TARGET_FREE_FRACTION = 0.12

PROBE_EVERY_TICKS = 5
PROBE_FILENAME = ".vidette-probe"
_PROBE_PAYLOAD = b"vidette write probe\n"

# Every class 'forever': expiry never fires, so a plan with this policy contains pressure
# deletions only (see module docstring).
_PRESSURE_ONLY_RETENTION = Retention(continuous=None, motion=None, events=None, favorites=None)


@dataclass
class JanitorStatus:
    last_run_at: float | None
    disk_total_bytes: int | None
    disk_free_bytes: int | None
    media_bytes: int | None
    last_probe_ok: bool | None
    expired_deleted_total: int
    pressure_deleted_total: int


def _row_to_segment(row: SegmentRow) -> Segment | None:
    """DB row → planner segment; unknown classes are skipped (never delete what we cannot
    classify)."""
    try:
        klass = SegmentClass(row.klass)
    except ValueError:
        logger.warning("segment %s has unknown class %r — skipping it", row.path, row.klass)
        return None
    return Segment(
        camera=row.camera,
        start=datetime.fromtimestamp(row.start_ts, tz=UTC),
        end=datetime.fromtimestamp(row.end_ts, tz=UTC),
        path=row.path,
        size_bytes=row.size_bytes,
        klass=klass,
    )


class Janitor:
    def __init__(
        self,
        config: VidetteConfig,
        db: Database,
        exporter: ExportManager,
        *,
        interval_s: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._db = db
        self._exporter = exporter
        self._interval_s = interval_s
        self._clock = clock
        self._task: asyncio.Task[None] | None = None

        self._ticks = 0
        self._last_run_at: float | None = None
        self._disk_total: int | None = None
        self._disk_free: int | None = None
        self._media_bytes: int | None = None
        self._last_probe_ok: bool | None = None
        self._expired_total = 0
        self._pressure_total = 0

        # Last-state flags: each watermark/failure event fires once per crossing, not per tick.
        self._low_active = False
        self._pressure_active = False
        self._probe_failed = False
        self._disk_stat_failed = False

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="vidette-janitor")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def run_once(self) -> JanitorStatus:
        """One full iteration — exposed for tests and a future admin 'run now' button."""
        now = self._clock()
        now_dt = datetime.fromtimestamp(now, tz=UTC)
        media_dir = self._config.storage.media_dir

        per_camera: dict[str, list[Segment]] = {}
        for row in await self._db.all_segments():
            segment = _row_to_segment(row)
            if segment is not None:
                per_camera.setdefault(segment.camera, []).append(segment)

        # 1) Per-camera expiry pass.
        deleted_paths: set[str] = set()
        for camera_id, segments in per_camera.items():
            camera = self._config.cameras.get(camera_id)
            retention = self._config.storage.retention
            if camera is not None and camera.record.retention is not None:
                retention = camera.record.retention
            plan = plan_deletions(segments, retention, now=now_dt)
            deleted = await self._delete_segments(plan.expired)
            deleted_paths.update(deleted)
            self._expired_total += len(deleted)

        # 2) Disk watermarks + global pressure pass.
        total, free = self._disk_usage(media_dir)
        self._disk_total, self._disk_free = total, free
        if total is not None and free is not None and total > 0:
            await self._check_watermarks(total, free, per_camera, deleted_paths, now_dt)
        elif total is None:
            await self._emit_once(
                "_disk_stat_failed",
                "storage.stat_failed",
                {
                    "media_dir": str(media_dir),
                    "action": "check that the media volume is mounted and readable",
                },
            )

        # 3) Write probe every 5th tick (tick 0 probes, so problems surface at boot).
        if self._ticks % PROBE_EVERY_TICKS == 0:
            ok, error = self._write_probe(media_dir)
            self._last_probe_ok = ok
            if ok:
                self._probe_failed = False
            else:
                await self._emit_once(
                    "_probe_failed",
                    "storage.write_failed",
                    {
                        "path": str(media_dir / PROBE_FILENAME),
                        "error": error,
                        "action": "check that the media volume is mounted, writable and "
                        "not out of inodes",
                    },
                )
        self._ticks += 1

        # 4) Housekeeping.
        await self._db.purge_expired_sessions(now)
        await self._exporter.cleanup_old()
        await self._db.checkpoint()  # keep the main DB file real (WAL → db, TRUNCATE)

        self._media_bytes = await self._db.media_bytes_total()
        self._last_run_at = now
        return self.status()

    def status(self) -> JanitorStatus:
        return JanitorStatus(
            last_run_at=self._last_run_at,
            disk_total_bytes=self._disk_total,
            disk_free_bytes=self._disk_free,
            media_bytes=self._media_bytes,
            last_probe_ok=self._last_probe_ok,
            expired_deleted_total=self._expired_total,
            pressure_deleted_total=self._pressure_total,
        )

    # --- internals --------------------------------------------------------------------------

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("janitor iteration failed; retrying in %.0f s", self._interval_s)
            await asyncio.sleep(self._interval_s)

    def _disk_usage(self, media_dir: Path) -> tuple[int | None, int | None]:
        try:
            usage = shutil.disk_usage(media_dir)
        except OSError as exc:
            logger.error("cannot stat media volume %s: %s", media_dir, exc)
            return None, None
        self._disk_stat_failed = False
        return usage.total, usage.free

    async def _check_watermarks(
        self,
        total: int,
        free: int,
        per_camera: dict[str, list[Segment]],
        deleted_paths: set[str],
        now_dt: datetime,
    ) -> None:
        free_fraction = free / total

        if free_fraction < WARN_FREE_FRACTION:
            await self._emit_once(
                "_low_active",
                "storage.low",
                {
                    "free_bytes": free,
                    "total_bytes": total,
                    "free_fraction": round(free_fraction, 4),
                    "threshold": WARN_FREE_FRACTION,
                    "action": "free disk space or shorten retention before recording is at risk",
                },
            )
        else:
            self._low_active = False

        if free_fraction >= PRESSURE_FREE_FRACTION:
            self._pressure_active = False
            return

        bytes_to_free = int(total * TARGET_FREE_FRACTION) - free
        survivors = [
            segment
            for segments in per_camera.values()
            for segment in segments
            if segment.path not in deleted_paths
        ]
        plan = plan_deletions(
            survivors, _PRESSURE_ONLY_RETENTION, now=now_dt, bytes_to_free=bytes_to_free
        )
        deleted = await self._delete_segments(plan.pressure)
        self._pressure_total += len(deleted)

        if plan.unmet_bytes > 0:
            await self._emit_once(
                "_pressure_active",
                "storage.pressure",
                {
                    "unmet_bytes": plan.unmet_bytes,
                    "bytes_to_free": bytes_to_free,
                    "free_bytes": free,
                    "total_bytes": total,
                    "action": "add disk space or shorten retention — event and favorite "
                    "footage is never auto-deleted",
                },
            )
        else:
            self._pressure_active = False

    async def _delete_segments(self, segments: list[Segment]) -> list[str]:
        """Unlink files, then remove their DB rows. Already-missing files count as deleted;
        a file that refuses to die keeps its row (so we retry) and raises a system event."""
        deleted: list[str] = []
        for segment in segments:
            try:
                Path(segment.path).unlink(missing_ok=True)
            except OSError as exc:
                await self._db.add_system_event(
                    "storage.delete_failed",
                    {
                        "path": segment.path,
                        "error": str(exc),
                        "action": "check permissions and mount options on the media directory",
                    },
                )
                continue
            deleted.append(segment.path)
        if deleted:
            await self._db.delete_segments_by_path(deleted)
        return deleted

    def _write_probe(self, media_dir: Path) -> tuple[bool, str | None]:
        probe = media_dir / PROBE_FILENAME
        try:
            probe.write_bytes(_PROBE_PAYLOAD)
            fd = os.open(probe, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            read_back = probe.read_bytes()
            probe.unlink()
        except OSError as exc:
            return False, str(exc)
        if read_back != _PROBE_PAYLOAD:
            return False, "read-back mismatch — the media volume returned different data"
        return True, None

    async def _emit_once(self, flag_attr: str, kind: str, payload: dict[str, object]) -> None:
        """Emit a system event on the False → True transition of the named flag only."""
        if getattr(self, flag_attr):
            return
        setattr(self, flag_attr, True)
        await self._db.add_system_event(kind, dict(payload))
