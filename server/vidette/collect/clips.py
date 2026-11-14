"""Turning a partial recording into a dataset clip: MP4, key frames, JSON sidecar.

Layout inside the dataset folder (the `vidette eval` format, docs/eval.md):

    clips/20261114-210304.mp4      the wake period or motion, stream-copied (no re-encode)
    clips/20261114-210304.json     ClipMeta: when, how long, codec, why it stopped
    frames/20261114-210304/1.jpg   4 to 8 key frames spread over the clip (≤ 1280 px)
    .partial/<epoch>.mkv.part      a recording in progress (or left by a crash)

With `--rotate`, the key frames are turned upright and the MP4 carries the rotation as
display metadata (players show it upright, the pixels are untouched).

The id is the local wall-clock time the media started, so it sorts, names the moment and
is a valid dataset entry id. Neither the file names nor the sidecar contain anything about
the camera's address or credentials.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from vidette import __version__
from vidette.ffmpeg import MediaToolError, extract_frames, frame_times, probe, remux_mp4

logger = logging.getLogger(__name__)

CLIPS_DIR = "clips"
FRAMES_DIR = "frames"
PARTIAL_DIR = ".partial"
PART_SUFFIX = ".mkv.part"
MIN_FRAMES, MAX_FRAMES = 4, 8


class MotionInfo(BaseModel):
    """What the motion gate saw (continuous mode only)."""

    active_s: float  # time covered by frames with motion
    peak_area: float  # largest changed share of the unmasked frame, 0..1
    capped: bool = False  # motion went on past --max; the clip is its first --max
    preroll_s: float
    postroll_s: float


class ClipMeta(BaseModel):
    """The sidecar next to every collected clip."""

    version: Literal[1] = 1
    id: str
    camera: str
    clip: str  # relative to the dataset root
    frames: list[str] = Field(default_factory=list)
    frame_times_s: list[float] = Field(default_factory=list)
    started_at: datetime  # local time with offset
    ended_at: datetime
    duration_s: float
    codec: str | None = None
    width: int | None = None  # as the camera sends it, before `rotation`
    height: int | None = None
    fps: float | None = None
    size_bytes: int = 0
    stop_reason: str
    source: Literal["rtsp"] = "rtsp"
    trigger: Literal["wake", "motion"] = "wake"  # camera woke up / continuous motion gate
    rotation: int = 0  # clockwise degrees applied to the key frames and the MP4's display
    motion: MotionInfo | None = None
    collector: str = f"vidette collect {__version__}"
    adopted: bool = False  # finalized from a partial file left by an interrupted run
    # The face check, added later (docs/people.md). Plain data here: a report this version
    # cannot read must never make the clip itself unreadable.
    people: dict[str, Any] | None = None


def frame_count(duration_s: float) -> int:
    """4 frames for short clips, one more per 8 s, at most 8."""
    return max(MIN_FRAMES, min(MAX_FRAMES, math.ceil(duration_s / 8) + 2))


def clip_id(started_at: float) -> str:
    return datetime.fromtimestamp(started_at).strftime("%Y%m%d-%H%M%S")


def _free_id(dataset: Path, base: str) -> str:
    candidate, n = base, 2
    while any((dataset / CLIPS_DIR / f"{candidate}{ext}").exists() for ext in (".mp4", ".mkv")):
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def partial_path(dataset: Path, started: float) -> Path:
    return dataset / PARTIAL_DIR / f"{int(started * 1000)}{PART_SUFFIX}"


def _write_json(path: Path, data: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)


async def finalize(
    part: Path,
    dataset: Path,
    *,
    camera: str,
    started_at: float,
    stop_reason: str,
    min_seconds: float,
    adopted: bool = False,
    rotation: int = 0,
    ended_at: float | None = None,
    trigger: Literal["wake", "motion"] = "wake",
    motion: MotionInfo | None = None,
) -> ClipMeta | None:
    """Promote `part` to a clip; None (and the part deleted) when it is too short or
    unreadable. Never raises for a bad recording: it is logged and dropped.

    `ended_at` anchors the clip at its end instead (a cut out of the ring starts on the
    key frame before the requested start, so the end is the precise side)."""
    try:
        info = await probe(part)
    except MediaToolError as exc:
        logger.warning("dropping an unreadable recording (%s)", exc)
        part.unlink(missing_ok=True)
        return None
    if info.duration_s < min_seconds or info.codec is None:
        logger.info(
            "dropping a %.1f s recording (shorter than --min %.0f s)", info.duration_s, min_seconds
        )
        part.unlink(missing_ok=True)
        return None

    if ended_at is not None:
        started_at = ended_at - info.duration_s
    entry_id = _free_id(dataset, clip_id(started_at))
    clips = dataset / CLIPS_DIR
    clips.mkdir(parents=True, exist_ok=True)
    clip = clips / f"{entry_id}.mp4"
    try:
        tagged = await remux_mp4(part, clip, hevc=info.codec in ("hevc", "h265"),
                                 rotation=rotation)
        if not tagged:
            logger.warning("this ffmpeg cannot tag %s with its rotation (needs 6.0 or newer); "
                           "the key frames are turned anyway", clip.name)
    except MediaToolError as exc:
        # Some camera codecs do not fit MP4; Matroska is a valid dataset format too.
        clip = clips / f"{entry_id}.mkv"
        logger.warning("keeping %s as Matroska: %s", clip.name, exc)
        part.replace(clip)
    part.unlink(missing_ok=True)

    times = frame_times(info.duration_s, frame_count(info.duration_s))
    frames: list[Path] = []
    try:
        frames = await extract_frames(
            clip, times, dataset / FRAMES_DIR / entry_id, rotation=rotation
        )
    except MediaToolError as exc:
        logger.warning("key frames for %s incomplete: %s", entry_id, exc)
        frames = sorted((dataset / FRAMES_DIR / entry_id).glob("*.jpg"))

    started = datetime.fromtimestamp(started_at).astimezone()
    meta = ClipMeta(
        id=entry_id,
        camera=camera,
        clip=clip.relative_to(dataset).as_posix(),
        frames=[frame.relative_to(dataset).as_posix() for frame in frames],
        frame_times_s=times[: len(frames)],
        started_at=started,
        ended_at=datetime.fromtimestamp(started_at + info.duration_s).astimezone(),
        duration_s=round(info.duration_s, 2),
        codec=info.codec,
        width=info.width,
        height=info.height,
        fps=info.fps,
        size_bytes=clip.stat().st_size,
        stop_reason=stop_reason,
        trigger=trigger,
        rotation=rotation,
        motion=motion,
        adopted=adopted,
    )
    _write_json(clips / f"{entry_id}.json", meta.model_dump_json(indent=2) + "\n")
    return meta


async def adopt_leftovers(
    dataset: Path,
    *,
    camera: str,
    min_seconds: float,
    rotation: int = 0,
    trigger: Literal["wake", "motion"] = "wake",
) -> list[ClipMeta]:
    """Finalize partial recordings an interrupted run left behind (crash, kill, reboot)."""
    adopted: list[ClipMeta] = []
    for part in sorted((dataset / PARTIAL_DIR).glob(f"*{PART_SUFFIX}")):
        stem = part.name.removesuffix(PART_SUFFIX)
        started = int(stem) / 1000 if stem.isdigit() else part.stat().st_mtime
        meta = await finalize(
            part,
            dataset,
            camera=camera,
            started_at=started,
            stop_reason="adopted after an interrupted run",
            min_seconds=min_seconds,
            adopted=True,
            rotation=rotation,
            trigger=trigger,
        )
        if meta is not None:
            adopted.append(meta)
    return adopted


def load_meta(path: Path) -> ClipMeta | None:
    try:
        return ClipMeta.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
