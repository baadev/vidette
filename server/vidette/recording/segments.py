"""Segment planning and ffmpeg command construction (pure, fully testable).

Recording format (ADR-0004): codec-copy MP4 segments, ~10 s, laid out
`<media_dir>/<camera>/YYYY/MM/DD/HH/<epoch>.mp4` (UTC). The recorder learns about finalized
segments from ffmpeg's `-segment_list pipe:1 -segment_list_type csv` stream: each CSV line
is `<filename>,<start_rel_s>,<end_rel_s>` (times relative to recording start). Absolute
segment start comes from the strftime `%s` epoch embedded in the filename; duration comes
from the CSV (end_rel - start_rel).

Command shape (implementation must match; tests pin it):
  ffmpeg -nostdin -hide_banner -loglevel warning
         -rtsp_transport tcp -i <source_url>
         -c copy -map 0
         -f segment -segment_time <seconds> -segment_atclocktime 1 -reset_timestamps 1
         -strftime 1 -strftime_mkdir 1
         -segment_format mp4 -segment_format_options movflags=+faststart
         -segment_list pipe:1 -segment_list_type csv
         <camera_dir>/%Y/%m/%d/%H/%s.mp4

Field notes (verified against ffmpeg 8.1):
- The *segment* muxer has no `strftime_mkdir` option (only hls does); ffmpeg accepts the
  flag without complaint but does not create directories. The recorder therefore
  pre-creates hour directories (see `segment_hour_dir` and CameraRecorder).
- ffmpeg expands `%Y/%m/%d/%H` with *local* time, `%s` with the absolute epoch; the two
  always agree, so the hour directory of a segment is derivable from its epoch stem.
- CSV list entries carry the segment *basename* (e.g. `1783430183.mp4`), not the full
  path — `parse_segment_list_line` reconstructs the absolute path from the epoch.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path

SEGMENT_SECONDS_DEFAULT = 10

#: strftime pattern appended to the camera directory; %s becomes the segment's start epoch.
SEGMENT_PATH_PATTERN = "%Y/%m/%d/%H/%s.mp4"


@dataclass(frozen=True)
class SegmentNotice:
    """A finalized segment, ready to index."""

    path: Path  # absolute
    start_ts: float  # unix epoch seconds
    end_ts: float
    size_bytes: int


def camera_media_dir(media_dir: Path, camera_id: str) -> Path:
    """<media_dir>/<camera_id>; camera_id must already be schema-validated ([a-z0-9-])."""
    return media_dir / camera_id


def segment_hour_dir(camera_dir: Path, epoch: float) -> Path:
    """Hour directory for a segment starting at `epoch`.

    Uses *local* time to match ffmpeg's strftime expansion of `%Y/%m/%d/%H` (the segment
    muxer expands the pattern with localtime; in the shipped container TZ=UTC, so the
    documented layout holds there).
    """
    tm = time.localtime(epoch)
    return (
        camera_dir
        / f"{tm.tm_year:04d}"
        / f"{tm.tm_mon:02d}"
        / f"{tm.tm_mday:02d}"
        / f"{tm.tm_hour:02d}"
    )


def build_record_command(
    source_url: str,
    camera_dir: Path,
    segment_seconds: int = SEGMENT_SECONDS_DEFAULT,
    *,
    input_args: tuple[str, ...] = ("-rtsp_transport", "tcp"),
) -> list[str]:
    """`input_args` precede `-i` — RTSP transport in production; tests substitute
    file/lavfi-friendly flags (e.g. ("-re", "-stream_loop", "-1")) to exercise the real
    pipeline without a camera."""
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "warning",
        *input_args,
        "-i",
        source_url,
        "-c",
        "copy",
        "-map",
        "0",
        "-f",
        "segment",
        "-segment_time",
        str(segment_seconds),
        "-segment_atclocktime",
        "1",
        "-reset_timestamps",
        "1",
        "-strftime",
        "1",
        "-strftime_mkdir",
        "1",
        "-segment_format",
        "mp4",
        "-segment_format_options",
        "movflags=+faststart",
        "-segment_list",
        "pipe:1",
        "-segment_list_type",
        "csv",
        str(camera_dir / SEGMENT_PATH_PATTERN),
    ]


def parse_segment_list_line(line: str, camera_dir: Path) -> SegmentNotice | None:
    """CSV line → SegmentNotice (stat()s the file for size). None for blank/malformed lines
    or files that don't exist; malformed lines must not raise (ffmpeg owns that pipe)."""
    text = line.strip()
    if not text:
        return None
    parts = text.split(",")
    if len(parts) != 3:
        return None
    filename, start_raw, end_raw = (part.strip() for part in parts)
    if not filename:
        return None
    try:
        start_rel = float(start_raw)
        end_rel = float(end_raw)
    except ValueError:
        return None
    if not (math.isfinite(start_rel) and math.isfinite(end_rel)):
        return None
    duration = end_rel - start_rel
    if duration < 0:
        return None

    candidate = Path(filename)
    if not candidate.stem.isdigit():
        return None
    start_epoch = int(candidate.stem)

    if candidate.is_absolute():
        path = candidate
    else:
        # ffmpeg lists the basename; the hour directory is derivable from the epoch stem.
        path = segment_hour_dir(camera_dir, start_epoch) / candidate.name
        if not path.exists():
            # Fallback for list styles that carry a path relative to the camera dir.
            path = camera_dir / candidate
    try:
        size_bytes = path.stat().st_size
    except OSError:
        return None
    return SegmentNotice(
        path=path,
        start_ts=float(start_epoch),
        end_ts=start_epoch + duration,
        size_bytes=size_bytes,
    )
