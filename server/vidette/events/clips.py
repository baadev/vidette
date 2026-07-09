"""Lazy event clips: remux the segments around an event into one MP4.

Same mechanics as the range exporter (concat demuxer, ``-c copy``, no re-encode) but a
deliberately small local implementation — an event clip is produced on first request by
the API, synchronously awaited, and cached at its final path. Failure is a plain `False`
(no footage, no ffmpeg, unsafe paths); the caller turns that into an actionable 404.

Safety: every input segment path *and* the output path must resolve under `media_dir` —
rows come from our own DB, but defense in depth is house policy.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from pathlib import Path

from vidette.db import Database

logger = logging.getLogger(__name__)

_STDERR_TAIL_CHARS = 500


def _concat_escape(path: Path) -> str:
    """Escape a path for a `file '<path>'` concat-list line (single quotes only)."""
    return str(path).replace("'", "'\\''")


async def materialize_clip(
    db: Database,
    media_dir: Path,
    camera: str,
    start_ts: float,
    end_ts: float,
    out_path: Path,
    *,
    pre_roll_s: float = 5.0,
    post_roll_s: float = 5.0,
    ffmpeg_path: str = "ffmpeg",
) -> bool:
    """Concat-remux the segments overlapping [start-pre_roll, end+post_roll] to `out_path`.

    Returns True when `out_path` now holds a playable MP4; False when there is nothing to
    remux, ffmpeg is unavailable, or any path steps outside `media_dir`.
    """
    if shutil.which(ffmpeg_path) is None:
        logger.warning("event clip skipped: ffmpeg '%s' not found on PATH", ffmpeg_path)
        return False

    media_root = media_dir.resolve()
    resolved_out = out_path.resolve()
    if not resolved_out.is_relative_to(media_root):
        logger.error("event clip output %s escapes the media directory — refusing", out_path)
        return False

    segments = await db.segments_between(camera, start_ts - pre_roll_s, end_ts + post_roll_s)
    if not segments:
        return False

    paths: list[Path] = []
    for row in segments:
        resolved = Path(row.path).resolve()
        if not resolved.is_relative_to(media_root):
            logger.error(
                "segment %s escapes the media directory — refusing to build the clip; "
                "check the database and media volume for tampering",
                row.path,
            )
            return False
        paths.append(resolved)

    resolved_out.parent.mkdir(parents=True, exist_ok=True)
    list_path = resolved_out.with_suffix(".txt")
    try:
        list_path.write_text(
            "".join(f"file '{_concat_escape(path)}'\n" for path in paths), encoding="utf-8"
        )
        return await _run_ffmpeg(ffmpeg_path, list_path, resolved_out)
    except OSError as exc:
        logger.warning("event clip for %s failed: %s", camera, exc)
        return False
    finally:
        with contextlib.suppress(OSError):
            list_path.unlink(missing_ok=True)


async def _run_ffmpeg(ffmpeg_path: str, list_path: Path, out_path: Path) -> bool:
    command = [
        ffmpeg_path,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",  # a retried materialization may find a stale partial file
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
        logger.warning("event clip skipped: ffmpeg '%s' vanished from PATH", ffmpeg_path)
        return False

    _, stderr = await process.communicate()
    if process.returncode == 0 and out_path.is_file():
        return True

    tail = stderr.decode("utf-8", errors="replace").strip()[-_STDERR_TAIL_CHARS:]
    logger.warning(
        "ffmpeg exited with code %s while building %s: %s",
        process.returncode,
        out_path,
        tail or "<no output>",
    )
    with contextlib.suppress(OSError):
        out_path.unlink(missing_ok=True)
    return False
