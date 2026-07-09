"""Timeline scrub-strip previews: tiny ~1 fps MP4s, one per camera per completed hour.

The Review timeline scrubs these instead of full-rate segments — that is what makes it
feel instant (docs/architecture/storage.md). Each fully-elapsed UTC hour that has recorded
footage gets `<media_dir>/<camera>/previews/<hour_epoch>.mp4`, produced from that hour's
segments via the ffmpeg concat demuxer and a low-res re-encode (`fps=1,scale=-2:180`,
libx264 ultrafast). This is the ONE place Vidette re-encodes; the output is tiny by design
(~1–2 % storage overhead).

Priority: preview generation is the lowest rung of the shedding ladder
(docs/architecture/overview.md) — Tier 3 sheds first, then Tier 1–2, then previews pause;
the recorder never sheds. The worker loop therefore sleeps `interval_s` (default 300 s)
between passes and runs generation jobs strictly sequentially — a missed pass just means
the timeline catches up a few minutes later.

Failure policy: a failed hour is logged, recorded in `status().last_error` and retried on
the next pass. The output only appears via `os.replace`, so a half-written preview is
never served. `run_once` never raises.
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
from pathlib import Path

from vidette.core.config import VidetteConfig
from vidette.db import Database

logger = logging.getLogger(__name__)

PREVIEWS_DIRNAME = "previews"
LOOKBACK_HOURS = 24
HOUR_S = 3600

#: 1 frame per second, 180 px tall, width kept even for yuv420p (`-2`).
_PREVIEW_FILTER = "fps=1,scale=-2:180"

_STDERR_TAIL_CHARS = 500

_FFMPEG_MISSING = (
    "ffmpeg not found — install ffmpeg (it ships in the official container) to enable "
    "timeline previews"
)


@dataclass
class PreviewStatus:
    last_run_at: float | None
    generated_total: int
    last_error: str | None


def preview_path(media_dir: Path, camera: str, hour_start_ts: float) -> Path:
    """`<media_dir>/<camera>/previews/<hour_epoch>.mp4`; camera ids are schema-validated
    (`[a-z0-9-]`), so the id can never traverse out of `media_dir`."""
    return media_dir / camera / PREVIEWS_DIRNAME / f"{int(hour_start_ts)}.mp4"


def _concat_escape(path: Path) -> str:
    """Escape a path for a `file '<path>'` concat-list line (single quotes only)."""
    return str(path).replace("'", "'\\''")


class PreviewWorker:
    def __init__(
        self,
        config: VidetteConfig,
        db: Database,
        *,
        media_dir: Path,
        interval_s: float = 300.0,
        ffmpeg_path: str = "ffmpeg",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._db = db
        self._media_dir = media_dir
        self._interval_s = interval_s
        self._ffmpeg = ffmpeg_path
        self._clock = clock
        self._task: asyncio.Task[None] | None = None

        self._last_run_at: float | None = None
        self._generated_total = 0
        self._last_error: str | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="vidette-previews")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def run_once(self) -> int:
        """One full pass — exposed for tests and a future admin 'run now' button.

        For every camera and every fully-elapsed UTC hour of the last 24 h: if the hour
        has indexed segments and no preview file yet, generate one. Returns the number of
        previews generated this pass; failures are recorded, never raised.
        """
        now = self._clock()
        if shutil.which(self._ffmpeg) is None:
            self._last_error = _FFMPEG_MISSING
            self._last_run_at = now
            return 0

        current_hour = int(now // HOUR_S) * HOUR_S  # start of the (incomplete) current hour
        generated = 0
        for camera_id in self._config.cameras:
            for back in range(1, LOOKBACK_HOURS + 1):
                hour_start = float(current_hour - back * HOUR_S)
                if preview_path(self._media_dir, camera_id, hour_start).exists():
                    continue
                try:
                    if await self.generate_hour(camera_id, hour_start) is not None:
                        generated += 1
                except Exception as exc:
                    logger.exception(
                        "preview generation failed for %s/%d", camera_id, int(hour_start)
                    )
                    self._last_error = f"preview {camera_id}/{int(hour_start)}: {exc}"

        self._generated_total += generated
        self._last_run_at = now
        return generated

    async def generate_hour(self, camera: str, hour_start_ts: float) -> Path | None:
        """Generate the preview for one camera-hour; returns its path, or None when the
        hour has no usable footage or generation failed (recorded in `status()`)."""
        rows = await self._db.segments_between(camera, hour_start_ts, hour_start_ts + HOUR_S)
        if not rows:
            return None

        # Defense in depth: every path the DB hands us must live under media_dir.
        media_root = self._media_dir.resolve()
        paths: list[Path] = []
        for row in rows:
            resolved = Path(row.path).resolve()
            if not resolved.is_relative_to(media_root):
                logger.warning(
                    "segment %s escapes the media directory — skipping it in preview %s/%d",
                    row.path,
                    camera,
                    int(hour_start_ts),
                )
                continue
            paths.append(resolved)
        if not paths:
            self._last_error = (
                f"preview {camera}/{int(hour_start_ts)}: every segment path escapes the "
                "media directory — check the database and media volume for tampering"
            )
            return None

        out_path = preview_path(self._media_dir, camera, hour_start_ts)
        list_path = out_path.with_suffix(".txt")
        tmp_path = out_path.with_name(f"{out_path.stem}.tmp.mp4")  # .mp4 so ffmpeg picks the muxer
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            list_path.write_text(
                "".join(f"file '{_concat_escape(path)}'\n" for path in paths),
                encoding="utf-8",
            )
            tmp_path.unlink(missing_ok=True)  # leftover from a crashed run; -nostdin can't ask
            if not await self._run_ffmpeg(camera, hour_start_ts, list_path, tmp_path):
                return None
            os.replace(tmp_path, out_path)  # atomic: readers only ever see a complete preview
        except OSError as exc:
            self._last_error = (
                f"preview {camera}/{int(hour_start_ts)}: {exc} — check that the media "
                "volume is mounted and writable"
            )
            logger.error("cannot write preview for %s/%d: %s", camera, int(hour_start_ts), exc)
            return None
        finally:
            with contextlib.suppress(OSError):
                list_path.unlink(missing_ok=True)
        return out_path

    def status(self) -> PreviewStatus:
        return PreviewStatus(
            last_run_at=self._last_run_at,
            generated_total=self._generated_total,
            last_error=self._last_error,
        )

    # --- internals --------------------------------------------------------------------------

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # run_once never raises; belt and braces for the task
                logger.exception("preview pass failed; retrying in %.0f s", self._interval_s)
            await asyncio.sleep(self._interval_s)

    async def _run_ffmpeg(
        self, camera: str, hour_start_ts: float, list_path: Path, out_path: Path
    ) -> bool:
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
            "-an",
            "-vf",
            _PREVIEW_FILTER,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
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
            self._last_error = _FFMPEG_MISSING
            return False

        _, stderr = await process.communicate()
        if process.returncode == 0:
            return True

        tail = stderr.decode("utf-8", errors="replace").strip()[-_STDERR_TAIL_CHARS:]
        self._last_error = (
            f"preview {camera}/{int(hour_start_ts)}: ffmpeg exited with code "
            f"{process.returncode}: {tail or '<no output>'}"
        )
        logger.error("%s", self._last_error)
        with contextlib.suppress(OSError):
            out_path.unlink(missing_ok=True)
        return False
