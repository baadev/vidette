"""Boot-time reconciliation: segment files on disk ↔ rows in the segments table.

Why: the recorder indexes a segment when ffmpeg announces it *finalized*. A container
recreate / crash between finalization batches leaves files on disk with no row — footage
that exists but is invisible in Review and, worse, invisible to retention (it would leak
disk forever). Field case: a deployment restart left 33 minutes of footage orphaned.

The reconciler runs per camera before its recorder starts (no writer is active then):

- an orphan that ffprobe can read is indexed with its real duration (`klass=continuous` —
  the recorder's own class for freshly announced segments; upgrades happen later anyway);
- an orphan ffprobe cannot read is a truncated tail ffmpeg never finalized (no moov atom):
  unrecoverable garbage, deleted so it cannot leak;
- files younger than `min_age_s` are skipped — never race a writer that may still own them.

Only the documented hour layout (`YYYY/MM/DD/HH/<epoch>.mp4`) is scanned; `previews/` and
anything else under the camera dir are not segment storage and are left alone.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from vidette.db import Database

logger = logging.getLogger(__name__)

_HOUR_LAYOUT_GLOB = "[0-9]*/[0-9]*/[0-9]*/[0-9]*/*.mp4"
_FFPROBE_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class ReconcileResult:
    indexed: int
    removed: int
    skipped: int  # too young or ffprobe unavailable/timed out — left for the next boot


async def _probe_duration(path: Path) -> float | None:
    """Container duration in seconds via ffprobe; None = unreadable (truncated tail)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        raise  # no ffprobe binary — caller skips the whole scan, not the file
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_FFPROBE_TIMEOUT_S)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return None
    if proc.returncode != 0:
        return None
    try:
        duration = float(stdout.decode("ascii", errors="replace").strip())
    except ValueError:
        return None
    return duration if duration > 0 else None


async def reconcile_camera_segments(
    db: Database,
    camera_id: str,
    camera_dir: Path,
    *,
    min_age_s: float = 60.0,
) -> ReconcileResult:
    """Index orphaned segment files; delete unreadable ones. Never raises past ffprobe
    absence (OSError) — callers treat that like missing ffmpeg (skip, warn once)."""
    if not camera_dir.is_dir():
        return ReconcileResult(0, 0, 0)
    known = await db.segment_paths(camera_id)
    now = time.time()
    indexed = removed = skipped = 0
    for path in sorted(camera_dir.glob(_HOUR_LAYOUT_GLOB)):
        if str(path) in known or not path.stem.isdigit():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue  # vanished mid-scan
        if now - stat.st_mtime < min_age_s:
            skipped += 1
            continue
        duration = await _probe_duration(path)
        if duration is None:
            logger.warning(
                "reconcile %s: removing unreadable orphan %s (truncated tail, no index row)",
                camera_id,
                path,
            )
            with contextlib.suppress(OSError):
                path.unlink()
            removed += 1
            continue
        start_ts = float(int(path.stem))
        await db.add_segment(
            camera=camera_id,
            start_ts=start_ts,
            end_ts=start_ts + duration,
            path=str(path),
            size_bytes=stat.st_size,
            klass="continuous",
        )
        indexed += 1
    if indexed or removed:
        logger.info(
            "reconcile %s: indexed %d orphaned segment(s), removed %d unreadable",
            camera_id,
            indexed,
            removed,
        )
    return ReconcileResult(indexed, removed, skipped)
