"""Which frames a VLM sees for one case.

- Key frames already on disk (a dataset entry, a collected clip) are used as they are:
  `count` of them, spread evenly over the list, first and last included.
- Otherwise `count` frames are taken from the first video, centred in equal slices of
  its duration (ffmpeg), into `cache_dir` when given so a second run costs nothing.
- Every frame is shrunk to `max_side` pixels on its long side and sent as JPEG: the cost
  of a hosted model grows with pixels, and a person at the door is still a person at
  768 px. Without ffmpeg (the `vidette serve` image), JPEG files are sent as they are.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from vidette.ffmpeg import MediaToolError, extract_frames, frame_times, jpeg, probe
from vidette.verdicts.base import Case

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv"})


def spread(items: list[Path], count: int) -> list[Path]:
    """`count` items evenly over the list, keeping the first and the last."""
    if count <= 0:
        return []
    if len(items) <= count:
        return list(items)
    if count == 1:
        return [items[len(items) // 2]]
    step = (len(items) - 1) / (count - 1)
    return [items[round(index * step)] for index in range(count)]


async def frame_files(case: Case, count: int, *, work_dir: Path) -> list[Path]:
    """Key frames on disk, or `count` frames extracted from the first video into
    `work_dir/<hash of the video>/` (reused when already there)."""
    images = [path for path in case.media if path.suffix.lower() in IMAGE_SUFFIXES]
    if images:
        return spread(images, count)
    videos = [path for path in case.media if path.suffix.lower() in VIDEO_SUFFIXES]
    if not videos:
        return []
    video = videos[0]
    stamp = f"{video.resolve()}:{video.stat().st_mtime_ns}:{count}"
    target = work_dir / hashlib.sha256(stamp.encode()).hexdigest()[:16]
    existing = sorted(target.glob("*.jpg"), key=lambda p: int(p.stem))
    if existing and len(existing) == count:
        return existing
    info = await probe(video)
    return await extract_frames(video, frame_times(info.duration_s, count), target)


async def frame_jpegs(
    case: Case, count: int, *, max_side: int, cache_dir: Path | None = None
) -> list[bytes]:
    """The frames to send, as JPEG bytes (possibly fewer than `count`, possibly none).

    Frames extracted from a video are kept in `cache_dir` when given, else in a
    temporary directory that is gone when this returns."""
    with tempfile.TemporaryDirectory(prefix="vidette-frames-") as tmp:
        paths = await frame_files(case, count, work_dir=cache_dir or Path(tmp))
        out: list[bytes] = []
        for path in paths:
            try:
                out.append(await jpeg(path, max_side=max_side))
            except MediaToolError:
                if path.suffix.lower() not in (".jpg", ".jpeg"):
                    raise
                out.append(path.read_bytes())  # no ffmpeg here: send the file as it is
        return out
