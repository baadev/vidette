"""Which frames a VLM sees for one case, and what it is told about their timing.

- A video in the case (a collected clip, a clip-folder clip) comes first. Frames are
  real, distinct frames of it, picked by its packets (`select_from_video`), never
  instants on its timeline: a stalled stream has no frame at most instants, and an
  instant inside a stall shows the frame before it again. Where the picture changed is
  found on tiny grey thumbnails of the key frames (no full decode: about half a second
  for two minutes of 1080p), and the frames are spread over the stretches that changed;
  with no change anywhere, over the whole clip. Each frame keeps its time in the clip,
  and the clip's stream health (vidette.streamhealth) says where the video paused, so the
  judge can be told both.
- Otherwise key frames already on disk (a dataset entry, Frigate snapshots) are used as
  they are: `count` of them, spread evenly over the list, first and last included.
- Frames are extracted at up to 1280 px and shrunk to `max_side` when sent: the cost of a
  hosted model grows with pixels, and a person at the door is still a person at 768 px.
  Without ffmpeg (the `vidette serve` image), JPEG files are sent as they are.
"""

from __future__ import annotations

import hashlib
import json
import logging
import statistics
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from vidette.ffmpeg import (
    MediaToolError,
    Packets,
    binary,
    extract_frame,
    extract_frames,
    frame_times,
    jpeg,
    packets,
    probe,
    run,
)
from vidette.streamhealth import StreamHealth, measure
from vidette.verdicts.base import Case, FrameSet

logger = logging.getLogger(__name__)

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv"})

THUMB_W, THUMB_H = 64, 36  # the activity map: 2304 grey pixels per candidate frame
MIN_CANDIDATES = 8  # fewer key frames than this: decode every n-th frame instead
MAX_CANDIDATES = 96
PIXEL_CHANGE = 18  # grey levels (of 255) a pixel must differ from the background by
ACTIVE_SHARE = 0.004  # a candidate shows change when this share of pixels changed (~9 px)
CACHE_FILE = "frames.json"

__all__ = [
    "Activity",
    "FrameSet",
    "activity_map",
    "frame_files",
    "frame_jpegs",
    "frame_set",
    "select_from_video",
    "select_with_activity",
    "spread",
]


def spread(items: list[Path], count: int) -> list[Path]:
    """`count` items evenly over the list, keeping the first and the last."""
    return [items[i] for i in spread_indices(len(items), count)]


def spread_indices(length: int, count: int) -> list[int]:
    """`count` indices evenly over range(length), first and last included."""
    if count <= 0 or length <= 0:
        return []
    if length <= count:
        return list(range(length))
    if count == 1:
        return [length // 2]
    step = (length - 1) / (count - 1)
    return [round(index * step) for index in range(count)]


def centred_indices(length: int, count: int) -> list[int]:
    """`count` indices into range(length), centred in equal slices (no edges: where
    something starts or ends to move is where least of it is in view)."""
    if count <= 0 or length <= 0:
        return []
    if length <= count:
        return list(range(length))
    return [int((index + 0.5) * length / count) for index in range(count)]


# --- choosing frames from a video -----------------------------------------------------------


@dataclass(frozen=True)
class Activity:
    """Where and how much the picture changed over a clip, from its tiny grey key frames
    (THUMB_W × THUMB_H): what the pre-filter (vidette.triage) reads next to the frames.

    `shares`: per candidate frame, the share of pixels that differ from the background
    once a global brightness change is taken out (`activity`). `shifts`: per candidate,
    that global change, in grey levels (a light switched on, a cloud, the switch to
    infrared). `cells`: the pixels of the thumbnail grid that changed in any active
    candidate, as indices into the THUMB_W × THUMB_H grid, row by row."""

    shares: list[float]
    shifts: list[float]
    cells: frozenset[int]

    @property
    def peak(self) -> float:
        return max(self.shares, default=0.0)

    @property
    def light(self) -> float:
        """The largest brightness change of a candidate from the background, 0 to 1."""
        return max((abs(s) for s in self.shifts), default=0.0) / 255.0

    @property
    def active(self) -> int:
        return sum(1 for share in self.shares if share >= ACTIVE_SHARE)


def activity_map(thumbs: list[bytes]) -> Activity:
    """`activity` with where it happened and the global brightness changes."""
    if not thumbs:
        return Activity([], [], frozenset())
    size = len(thumbs[0])
    if size == 0 or any(len(thumb) != size for thumb in thumbs):
        return Activity([0.0] * len(thumbs), [0.0] * len(thumbs), frozenset())
    background = [int(statistics.median(column)) for column in zip(*thumbs, strict=True)]
    bg_mean = sum(background) / size
    shares: list[float] = []
    shifts: list[float] = []
    cells: set[int] = set()
    for thumb in thumbs:
        shift = sum(thumb) / size - bg_mean
        changed = [
            index for index, (value, base) in enumerate(zip(thumb, background, strict=True))
            if abs(value - base - shift) > PIXEL_CHANGE
        ]  # fmt: skip
        share = len(changed) / size
        shares.append(share)
        shifts.append(round(shift, 2))
        if share >= ACTIVE_SHARE:
            cells.update(changed)
    return Activity(shares, shifts, frozenset(cells))


def activity(thumbs: list[bytes]) -> list[float]:
    """Per thumbnail, the share of its pixels that differ from the background (the
    per-pixel median of all of them) once a global brightness change is taken out."""
    return activity_map(thumbs).shares


def episodes(
    frame_count: int, candidates: list[int], scores: list[float]
) -> list[tuple[list[int], float]]:
    """Runs of consecutive active candidates, as (frame indices, peak activity). Each
    active candidate stands for the frames up to the next candidate."""
    bounds = [*candidates, frame_count]
    runs: list[tuple[list[int], float]] = []
    frames: list[int] = []
    peak = 0.0
    for position, (start, score) in enumerate(zip(candidates, scores, strict=True)):
        if score >= ACTIVE_SHARE:
            frames.extend(range(start, bounds[position + 1]))
            peak = max(peak, score)
        elif frames:
            runs.append((frames, peak))
            frames, peak = [], 0.0
    if frames:
        runs.append((frames, peak))
    return runs


def choose(
    frame_count: int, candidates: list[int], scores: list[float], count: int
) -> tuple[list[int], str]:
    """Frame indices (into the clip's frames, in presentation order) to show the judge.

    The picture changed in one or more episodes (`episodes`). Each episode shown gets
    two frames, so the judge sees how things moved; when there are more episodes than
    that allows, the longest ones are shown (someone working at a door lasts longer than
    a car's headlights sweeping past). Frames left over go to the longest episodes. Nothing
    changed anywhere: frames spread over the whole clip."""
    if frame_count <= 0 or count <= 0:
        return [], "spread"
    runs = episodes(frame_count, candidates, scores)
    if not runs:
        return centred_indices(frame_count, count), "spread"
    per = 2 if count >= 2 else 1
    longest = sorted(range(len(runs)), key=lambda i: (len(runs[i][0]), runs[i][1]), reverse=True)
    shown = sorted(longest[: max(1, count // per)])
    share = {i: min(per, len(runs[i][0])) for i in shown}
    left = count - sum(share.values())
    while left > 0:
        room = [i for i in shown if share[i] < len(runs[i][0])]
        if not room:
            break
        best = max(room, key=lambda i: len(runs[i][0]) / (share[i] + 1))
        share[best] += 1
        left -= 1
    chosen: list[int] = []
    for i in shown:
        frames = runs[i][0]
        chosen.extend(frames[j] for j in centred_indices(len(frames), share[i]))
    return sorted(chosen), "activity"


async def _thumbs(video: Path, *, every: int | None, expected: int) -> list[bytes]:
    """Tiny grey frames: every key frame (`every` None), or every `every`-th frame."""
    scale = f"scale={THUMB_W}:{THUMB_H},format=gray"
    if every is None:
        args = ["-skip_frame", "nokey", "-i", str(video), "-vf", scale]
    else:
        args = ["-i", str(video), "-vf", f"select='not(mod(n\\,{every}))',{scale}"]
    rc, stdout, stderr = await run(
        [
            binary("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error",
            "-threads", "2", *args,
            "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
        ],
        timeout=120,
    )  # fmt: skip
    size = THUMB_W * THUMB_H
    if rc != 0 or not stdout:
        raise MediaToolError(f"cannot decode thumbnails of {video.name}: {stderr.strip()[-200:]}")
    thumbs = [stdout[i : i + size] for i in range(0, len(stdout) - size + 1, size)]
    if len(thumbs) != expected:
        raise MediaToolError(f"{video.name}: {len(thumbs)} thumbnails for {expected} frames")
    return thumbs


async def candidate_scores(video: Path, found: Packets) -> tuple[list[int], list[float]]:
    """Candidate frame indices and their activity: the key frames when there are enough
    (cheap to decode), otherwise every n-th frame."""
    candidates, mapped = await candidate_activity(video, found)
    return candidates, mapped.shares


async def candidate_activity(video: Path, found: Packets) -> tuple[list[int], Activity]:
    """`candidate_scores` with the whole activity map (`activity_map`)."""
    keys = [index for index, key in enumerate(found.keys) if key]
    if len(keys) >= MIN_CANDIDATES:
        try:
            thumbs = await _thumbs(video, every=None, expected=len(keys))
        except MediaToolError as exc:
            logger.info("key-frame thumbnails failed (%s); decoding every n-th frame", exc)
        else:
            if len(keys) > MAX_CANDIDATES:
                picked = spread_indices(len(keys), MAX_CANDIDATES)
                keys, thumbs = [keys[i] for i in picked], [thumbs[i] for i in picked]
            return keys, activity_map(thumbs)
    total = len(found.times)
    every = max(1, -(-total // MAX_CANDIDATES))
    indices = list(range(0, total, every))
    thumbs = await _thumbs(video, every=every, expected=len(indices))
    return indices, activity_map(thumbs)


async def select_from_video(
    video: Path, count: int, out_dir: Path, *, max_side: int = 1280
) -> FrameSet:
    """Up to `count` real frames of `video` where the picture changed, as numbered JPEGs
    in `out_dir`, with their times, the clip's duration and its stream health."""
    frames, _activity = await select_with_activity(video, count, out_dir, max_side=max_side)
    return frames


async def select_with_activity(
    video: Path, count: int, out_dir: Path, *, max_side: int = 1280
) -> tuple[FrameSet, Activity | None]:
    """`select_from_video`, and the activity map the frames were chosen from (None when
    the clip has too few frames or its thumbnails could not be decoded)."""
    info = await probe(video)
    found = await packets(video)
    out_dir.mkdir(parents=True, exist_ok=True)
    if len(found.times) < 2:  # nothing to choose from: the old way, instants on the timeline
        instants = frame_times(info.duration_s, count)
        files = await extract_frames(video, instants, out_dir, max_side=max_side)
        return FrameSet(files, [*instants[: len(files)]], "spread", info.duration_s), None
    health = measure(found.times, info.fps)
    mapped: Activity | None = None
    try:
        candidates, mapped = await candidate_activity(video, found)
        chosen, method = choose(len(found.times), candidates, mapped.shares, count)
    except MediaToolError as exc:
        logger.info("no activity map for %s (%s); frames spread over the clip", video.name, exc)
        chosen, method = centred_indices(len(found.times), count), "spread"
    origin = found.times[0]
    paths: list[Path] = []
    times: list[float | None] = []
    for number, index in enumerate(chosen, start=1):
        at = found.times[index] - info.start_s
        # an accurate seek gives the first frame at or after the instant: this one
        path = await extract_frame(
            video, max(0.0, at - 0.0005), out_dir / f"{number}.jpg", max_side=max_side
        )
        paths.append(path)
        times.append(round(found.times[index] - origin, 2))
    return FrameSet(paths, times, method, info.duration_s, health), mapped


# --- the frames of a case -------------------------------------------------------------------


def load_cached(target: Path) -> FrameSet | None:
    """A frame set stored by `store` in `target`, if complete."""
    try:
        data = json.loads((target / CACHE_FILE).read_text(encoding="utf-8"))
        paths = [target / str(name) for name in data["frames"]]
        if not paths or not all(path.is_file() for path in paths):
            return None
        health = StreamHealth.model_validate(data["health"]) if data.get("health") else None
        times = [None if t is None else float(t) for t in data["times"]]
        duration = data.get("duration_s")
        return FrameSet(
            paths, times, str(data["method"]), None if duration is None else float(duration),
            health,
        )  # fmt: skip
    except (OSError, ValueError, KeyError, TypeError, ValidationError):
        return None


def store(target: Path, frames: FrameSet) -> None:
    data = {
        "frames": [path.name for path in frames.paths],
        "times": frames.times,
        "method": frames.method,
        "duration_s": frames.duration_s,
        "health": None if frames.health is None else frames.health.model_dump(mode="json"),
    }
    (target / CACHE_FILE).write_text(json.dumps(data) + "\n", encoding="utf-8")


def first_video(case: Case) -> Path | None:
    return next((p for p in case.media if p.suffix.lower() in VIDEO_SUFFIXES), None)


async def frame_set(
    case: Case, count: int, *, work_dir: Path, out_dir: Path | None = None
) -> FrameSet:
    """The frames for `case`: its own when already chosen; from its video into
    `out_dir`, or `work_dir/<hash of the video>/` reused when already there; else its
    images on disk. A video that cannot be read falls back to the images."""
    if case.frames is not None:
        return case.frames
    video = first_video(case)
    images = [path for path in case.media if path.suffix.lower() in IMAGE_SUFFIXES]
    if video is not None and count > 0:
        try:
            if out_dir is None:
                stamp = f"{video.resolve()}:{video.stat().st_mtime_ns}:{count}:v2"
                out_dir = work_dir / hashlib.sha256(stamp.encode()).hexdigest()[:16]
                cached = load_cached(out_dir)
                if cached is not None and len(cached.paths) <= count:
                    return cached
            frames = await select_from_video(video, count, out_dir)
            store(out_dir, frames)
            return frames
        except (MediaToolError, OSError) as exc:
            if not images:
                raise
            logger.info("frames from %s failed (%s); the key frames on disk", video.name, exc)
    return FrameSet(spread(images, count))


async def frame_files(case: Case, count: int, *, work_dir: Path) -> list[Path]:
    """The frame files for `case` (see `frame_set`)."""
    return (await frame_set(case, count, work_dir=work_dir)).paths


async def to_jpegs(paths: list[Path], *, max_side: int) -> list[bytes]:
    out: list[bytes] = []
    for path in paths:
        try:
            out.append(await jpeg(path, max_side=max_side))
        except MediaToolError:
            if path.suffix.lower() not in (".jpg", ".jpeg"):
                raise
            out.append(path.read_bytes())  # no ffmpeg here: send the file as it is
    return out


async def frame_jpegs(
    case: Case, count: int, *, max_side: int, cache_dir: Path | None = None
) -> list[bytes]:
    """The frames to send, as JPEG bytes (possibly fewer than `count`, possibly none).

    Frames extracted from a video are kept in `cache_dir` when given, else in a
    temporary directory that is gone when this returns."""
    with tempfile.TemporaryDirectory(prefix="vidette-frames-") as tmp:
        frames = await frame_set(case, count, work_dir=cache_dir or Path(tmp))
        return await to_jpegs(frames.paths, max_side=max_side)
