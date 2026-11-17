"""What the pre-filter looks at, for one clip: cheap facts, gathered once, kept as data.

- **The sidecar** of a `vidette collect` clip: how long, why it was cut (`wake`: the
  camera woke up; `motion`: the continuous mode's gate), and what the gate measured
  (`motion.peak_area`, `motion.active_s`).
- **The activity map** (vidette.verdicts.frames): the clip's key frames as 64×36 grey
  thumbnails, no full decode. How much changed and where, once a global brightness
  change is taken out, and how large that brightness change was (a light, a cloud, the
  switch to infrared).
- **Detections**: the object detector (vidette.triage.detector) on `frames` frames where
  the picture changed, plus the key frames the collector already wrote (spread over the
  clip, nothing to decode). Every detection down to a low score is kept, so the rules can
  tell a parked car (in every frame, same place) from one that moves, and so a replay
  can try other thresholds without looking again.

An `Observation` is plain data: it is stored on the event (`intent.triage.observed`) and
in `vidette triage replay`'s cache, and the rules (vidette.triage.rules) read nothing
else. Nothing here decides anything.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import shutil
import tempfile
import time
import zlib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from vidette.collect.clips import load_meta
from vidette.ffmpeg import MediaToolError, sample_frames
from vidette.triage.detector import Detection, ObjectDetector
from vidette.verdicts.base import Case
from vidette.verdicts.frames import (
    IMAGE_SUFFIXES,
    THUMB_H,
    THUMB_W,
    Activity,
    first_video,
    select_with_activity,
)

logger = logging.getLogger(__name__)

OBSERVATION_VERSION = 1
GRID = (THUMB_W, THUMB_H)
DENSE_EVERY_S = 4.0  # nothing changed on the map: a frame this often, so a far walker...
DENSE_MAX = 24  # ...visible for a few seconds is still in one of them
DENSE_SIDE = 640  # the detector's own input size: more pixels would be thrown away
DENSE_UNLESS = 0.2  # a person this likely already seen: no dense look needed


class SeenFrame(BaseModel):
    """One frame the detector looked at: where it came from, when, what it found."""

    src: str  # "activity" (chosen where the picture changed) or "key" (the collector's)
    t: float | None = None  # seconds into the clip, when known
    dets: list[list[Any]] = Field(default_factory=list)  # Detection.as_list()

    def detections(self) -> list[Detection]:
        return [d for d in (Detection.from_list(raw) for raw in self.dets) if d is not None]


class Observation(BaseModel):
    v: int = OBSERVATION_VERSION
    detector: str | None = None  # None: no detector check happened
    duration_s: float | None = None
    trigger: str | None = None  # the sidecar's: "wake" or "motion"
    motion_peak: float | None = None  # the gate's largest changed share, 0..1
    motion_active_s: float | None = None
    candidates: int = 0  # key frames on the activity map
    activity_peak: float | None = None  # largest changed share of a candidate, 0..1
    activity_active: int = 0  # candidates with change
    light: float | None = None  # largest global brightness change, 0..1
    cells: str | None = None  # where it changed: the 64×36 grid as a packed bitmap
    frames: list[SeenFrame] = Field(default_factory=list)
    dense: bool = False  # nothing changed on the map: also a frame every DENSE_EVERY_S
    error: str | None = None  # why the look was incomplete
    seconds: float = 0.0

    @property
    def checked(self) -> bool:
        """The detector looked at frames of this clip without an error."""
        return self.detector is not None and self.error is None and bool(self.frames)

    def cell_set(self) -> frozenset[int]:
        return unpack_cells(self.cells)


def pack_cells(cells: frozenset[int] | set[int]) -> str:
    """The changed cells of the 64×36 grid as a bitmap, compressed and base64-encoded
    (mostly a few dozen characters: most of a picture does not change)."""
    size = THUMB_W * THUMB_H
    bits = bytearray((size + 7) // 8)
    for cell in cells:
        if 0 <= cell < size:
            bits[cell // 8] |= 1 << (cell % 8)
    return base64.b64encode(zlib.compress(bytes(bits), 9)).decode("ascii")


def unpack_cells(packed: str | None) -> frozenset[int]:
    if not packed:
        return frozenset()
    try:
        bits = zlib.decompress(base64.b64decode(packed, validate=True))
    except (ValueError, zlib.error):
        try:  # the first replay caches wrote plain hex
            bits = bytes.fromhex(packed)
        except ValueError:
            return frozenset()
    return frozenset(
        index * 8 + bit for index, byte in enumerate(bits) for bit in range(8) if byte >> bit & 1
    )


def _sidecar_facts(video: Path | None, observation: Observation) -> None:
    if video is None:
        return
    meta = load_meta(video.with_suffix(".json"))
    if meta is None:
        return
    observation.duration_s = meta.duration_s
    observation.trigger = meta.trigger
    if meta.motion is not None:
        observation.motion_peak = meta.motion.peak_area
        observation.motion_active_s = meta.motion.active_s


def _activity_facts(activity: Activity, observation: Observation) -> None:
    observation.candidates = len(activity.shares)
    observation.activity_peak = round(activity.peak, 4)
    observation.activity_active = activity.active
    observation.light = round(activity.light, 4)
    observation.cells = pack_cells(activity.cells)


def _detect_all(detector: ObjectDetector, paths: list[Path]) -> list[list[Detection]]:
    return [detector.detect(path.read_bytes()) for path in paths]


async def observe(
    case: Case,
    detector: ObjectDetector | None,
    *,
    frames: int = 8,
    key_frames: bool = True,
    work_dir: Path | None = None,
    max_side: int = 960,
) -> Observation:
    """Look at one case. Never raises for a bad clip: what failed is in `error`, and an
    observation with an error is never enough to skip a clip (vidette.triage.rules)."""
    started = time.monotonic()
    observation = Observation(detector=None if detector is None else detector.name)
    video = first_video(case)
    keyed = [p for p in case.media if p.suffix.lower() in IMAGE_SUFFIXES] if key_frames else []
    _sidecar_facts(video, observation)
    temp = Path(tempfile.mkdtemp(prefix="vidette-triage-", dir=work_dir))
    try:
        chosen: list[tuple[str, Path, float | None]] = []
        if video is not None:
            try:
                picked, activity = await select_with_activity(
                    video, frames, temp, max_side=max_side
                )
            except (MediaToolError, OSError) as exc:
                observation.error = f"frames: {exc}"[:200]
            else:
                if observation.duration_s is None:
                    observation.duration_s = picked.duration_s
                if activity is not None:
                    _activity_facts(activity, observation)
                chosen += [("activity", path, at) for path, at in picked.timed()]
        chosen += [("key", path, None) for path in keyed if path.is_file()]
        if not chosen and observation.error is None:
            observation.error = "no frames"
        if detector is not None and chosen:
            try:
                found = await asyncio.to_thread(_detect_all, detector, [p for _s, p, _t in chosen])
            except Exception as exc:  # a detector failure is a fact, never a crash
                observation.error = f"detector: {type(exc).__name__}: {exc}"[:200]
            else:
                observation.frames = [
                    SeenFrame(src=src, t=at, dets=[d.as_list() for d in dets])
                    for (src, _path, at), dets in zip(chosen, found, strict=True)
                ]
        if detector is not None and video is not None and observation.checked:
            await look_densely(video, detector, observation, work_dir=temp / "dense")
    finally:
        with contextlib.suppress(OSError):
            shutil.rmtree(temp)
    observation.seconds = round(time.monotonic() - started, 2)
    return observation


def wants_dense(observation: Observation) -> bool:
    """Nothing changed on the activity map (the frames were spread over the clip), the
    clip is long enough for them to be far apart, and nobody was seen in them."""
    if observation.dense or observation.activity_active or observation.duration_s is None:
        return False
    spread = [f for f in observation.frames if f.src == "activity"]
    if observation.duration_s <= DENSE_EVERY_S * max(1, len(spread)):
        return False
    return not any(
        d.kind == "person" and d.score >= DENSE_UNLESS
        for frame in observation.frames
        for d in frame.detections()
    )


async def look_densely(
    video: Path, detector: ObjectDetector, observation: Observation, *, work_dir: Path
) -> None:
    """A frame every DENSE_EVERY_S over a clip where the activity map saw nothing: the
    map's 64×36 grid cannot see a person far away, and frames spread 15 s apart over a
    two-minute wake period miss one walking past. One ffmpeg run at the detector's size;
    the frames are added to the observation as `dense`."""
    if not wants_dense(observation):
        return
    started = time.monotonic()
    try:
        paths = await sample_frames(
            video,
            work_dir,
            fps=1.0 / DENSE_EVERY_S,
            max_frames=DENSE_MAX,
            max_side=DENSE_SIDE,
        )
        found = await asyncio.to_thread(_detect_all, detector, paths)
    except Exception as exc:  # the first look stands; this one is extra
        logger.info("dense look at %s failed: %s", video.name, exc)
        return
    observation.frames += [
        SeenFrame(src="dense", t=round(index * DENSE_EVERY_S, 2), dets=[d.as_list() for d in dets])
        for index, dets in enumerate(found)
    ]
    observation.dense = True
    observation.seconds = round(observation.seconds + time.monotonic() - started, 2)
