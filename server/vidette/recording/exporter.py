"""Range export (remux, no re-encode).

Design (docs/api.md, docs/events-and-automations.md): POST a range → async job → MP4.
Implementation notes:

- Overlapping segments come from `Database.segments_between`; the output is produced with
  the ffmpeg concat demuxer: a temp list file of `file '<abs path>'` lines + `-f concat
  -safe 0 -i <list> -c copy -movflags +faststart <out>.mp4`.
- Precision is keyframe/segment granularity at M1 (documented; no re-encode, ever).
- Safety: camera ids validated against config; every segment path must resolve under
  `media_dir` (paths come from our DB, but verify anyway — defense in depth); output lives
  under `<media_dir>/exports/<job_id>.mp4`; job ids are server-generated (never from user
  input); ranges longer than `MAX_RANGE_S` are rejected with an actionable error.
- Jobs run one at a time (asyncio queue) — export must never starve the recorder (shedding
  ladder). Old exports are removed by the janitor after 24 h.
- The job registry is in-memory: a server restart forgets job records (a poll for an old id
  returns 404/None). Orphaned files are still reclaimed — `cleanup_old` sweeps the exports
  directory by file mtime, so nothing leaks across restarts.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from vidette.core.config import VidetteConfig
from vidette.db import Database

logger = logging.getLogger(__name__)

MAX_RANGE_S = 24 * 3600

_STDERR_TAIL_CHARS = 500

ExportState = Literal["queued", "running", "done", "error"]


@dataclass
class ExportJob:
    id: str
    camera: str
    start_ts: float
    end_ts: float
    state: ExportState
    created_at: float
    path: Path | None = None
    error: str | None = None
    size_bytes: int | None = None


class ExportError(Exception):
    """User-actionable export failure (bad range, no footage, ffmpeg missing…)."""


def _concat_escape(path: Path) -> str:
    """Escape a path for a `file '<path>'` concat-list line (single quotes only)."""
    return str(path).replace("'", "'\\''")


class ExportManager:
    def __init__(
        self,
        config: VidetteConfig,
        db: Database,
        *,
        media_dir: Path,
        ffmpeg_path: str = "ffmpeg",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._db = db
        self._media_dir = media_dir
        self._exports_dir = media_dir / "exports"
        self._exports_dir.mkdir(parents=True, exist_ok=True)
        self._ffmpeg = ffmpeg_path
        self._clock = clock
        self._jobs: dict[str, ExportJob] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the single worker task."""
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._worker_loop(), name="vidette-export-worker")

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None

    async def create(self, camera: str, start_ts: float, end_ts: float) -> ExportJob:
        """Validate + enqueue. Raises ExportError with a message the UI can show verbatim."""
        if camera not in self._config.cameras:
            valid = ", ".join(sorted(self._config.cameras)) or "<none configured>"
            raise ExportError(f"unknown camera '{camera}' — configured cameras: {valid}")
        duration = end_ts - start_ts
        if duration <= 0:
            raise ExportError("export range is empty — 'end' must be after 'start'")
        if duration > MAX_RANGE_S:
            raise ExportError(
                f"export range is {duration:.0f} s — the maximum is {MAX_RANGE_S} s (24 h); "
                "request a shorter range"
            )
        if shutil.which(self._ffmpeg) is None:
            raise ExportError(
                "ffmpeg not found — install ffmpeg (it ships in the official container) "
                "to enable exports"
            )
        segments = await self._db.segments_between(camera, start_ts, end_ts)
        if not segments:
            raise ExportError("no recorded footage in that range")

        job = ExportJob(
            id=secrets.token_hex(8),
            camera=camera,
            start_ts=start_ts,
            end_ts=end_ts,
            state="queued",
            created_at=self._clock(),
        )
        self._jobs[job.id] = job
        self._queue.put_nowait(job.id)
        return job

    def get(self, job_id: str) -> ExportJob | None:
        return self._jobs.get(job_id)

    async def cleanup_old(self, older_than_s: float = 24 * 3600) -> int:
        """Remove finished job records + files older than the cutoff; returns count.

        Counts removed job records plus orphaned export files (leftovers from before a
        restart, swept by mtime) — the janitor calls this every iteration.
        """
        cutoff = self._clock() - older_than_s
        removed = 0

        for job_id, job in list(self._jobs.items()):
            if job.state in ("done", "error") and job.created_at < cutoff:
                if job.path is not None:
                    try:
                        job.path.unlink(missing_ok=True)
                    except OSError as exc:
                        logger.warning("could not remove old export %s: %s", job.path, exc)
                        continue  # keep the record; retry next janitor pass
                del self._jobs[job_id]
                removed += 1

        # Orphan sweep: files with no in-memory job (registry is lost on restart).
        known = {job.path for job in self._jobs.values() if job.path is not None}
        for pattern in ("*.mp4", "*.txt"):
            for file in self._exports_dir.glob(pattern):
                if file in known:
                    continue
                try:
                    if file.stat().st_mtime < cutoff:
                        file.unlink(missing_ok=True)
                        removed += 1
                except OSError as exc:
                    logger.warning("could not sweep old export file %s: %s", file, exc)
        return removed

    # --- worker ---------------------------------------------------------------------------

    async def _worker_loop(self) -> None:
        while True:
            job_id = await self._queue.get()
            job = self._jobs.get(job_id)
            try:
                if job is not None:
                    await self._run_job(job)
            except asyncio.CancelledError:
                if job is not None and job.state in ("queued", "running"):
                    job.state = "error"
                    job.error = "export interrupted by server shutdown — request it again"
                raise
            except Exception as exc:
                logger.exception("export job %s failed unexpectedly", job_id)
                if job is not None:
                    job.state = "error"
                    job.error = f"export failed unexpectedly: {exc}"
            finally:
                self._queue.task_done()

    async def _run_job(self, job: ExportJob) -> None:
        job.state = "running"

        segments = await self._db.segments_between(job.camera, job.start_ts, job.end_ts)
        if not segments:
            job.state = "error"
            job.error = (
                "no recorded footage in that range — it may have been deleted by retention "
                "since the export was requested"
            )
            return

        # Defense in depth: every path the DB hands us must live under media_dir.
        media_root = self._media_dir.resolve()
        paths: list[Path] = []
        for row in segments:
            resolved = Path(row.path).resolve()
            if not resolved.is_relative_to(media_root):
                job.state = "error"
                job.error = (
                    "a segment path escapes the media directory — refusing to export; "
                    "check the database and media volume for tampering"
                )
                return
            paths.append(resolved)

        self._exports_dir.mkdir(parents=True, exist_ok=True)
        out_path = self._exports_dir / f"{job.id}.mp4"
        list_path = self._exports_dir / f"{job.id}.txt"
        try:
            list_path.write_text(
                "".join(f"file '{_concat_escape(path)}'\n" for path in paths),
                encoding="utf-8",
            )
            await self._run_ffmpeg(job, list_path, out_path)
        except OSError as exc:
            job.state = "error"
            job.error = f"cannot write to the exports directory: {exc} — check the media volume"
        finally:
            with contextlib.suppress(OSError):
                list_path.unlink(missing_ok=True)

    async def _run_ffmpeg(self, job: ExportJob, list_path: Path, out_path: Path) -> None:
        command = [
            self._ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            job.state = "error"
            job.error = "ffmpeg not found — install ffmpeg and restart the server"
            return

        _, stderr = await process.communicate()
        if process.returncode == 0:
            job.path = out_path
            job.size_bytes = out_path.stat().st_size
            job.state = "done"
            return

        tail = stderr.decode("utf-8", errors="replace").strip()[-_STDERR_TAIL_CHARS:]
        job.state = "error"
        job.error = f"ffmpeg exited with code {process.returncode}: {tail or '<no output>'}"
        with contextlib.suppress(OSError):
            out_path.unlink(missing_ok=True)
