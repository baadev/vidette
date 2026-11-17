"""Where and when people are in a clip: the detector's person boxes with their times.

The pre-filter keeps the person detections of the frames it looked at on the event
(`intent.triage.observed.persons`); a long clip judged in person-centred windows
(vidette.verdicts.windows) keeps its denser track (`intent.windows.track`). Both are
read back here as `Sighting`s. `person_track` makes a dense track on demand: one ffmpeg
run at the detector's input size, a frame every `every_s`, the detector on each (about a
tenth of a second a frame on an Apple-silicon CPU).

Times are seconds into the clip; boxes are fractions of the upright frame (as players
show it), the same for every source.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vidette.ffmpeg import sample_frames
from vidette.triage.detector import ObjectDetector

Box = tuple[float, float, float, float]
TRACK_EVERY_S = 1.0
TRACK_MAX_FRAMES = 150
TRACK_SIDE = 640  # the detector's own input size
MIN_SCORE = 0.3


@dataclass(frozen=True)
class Sighting:
    t: float  # seconds into the clip
    score: float
    box: Box  # x, y, width, height as fractions of the upright frame

    def as_list(self) -> list[float]:
        return [round(self.t, 2), round(self.score, 3), *(round(v, 4) for v in self.box)]

    @classmethod
    def from_list(cls, raw: object) -> Sighting | None:
        if not isinstance(raw, list | tuple) or len(raw) != 6:
            return None
        try:
            t, score, x, y, w, h = (float(v) for v in raw)
        except (TypeError, ValueError):
            return None
        return cls(t, score, (x, y, w, h))

    @property
    def area(self) -> float:
        return self.box[2] * self.box[3]

    @property
    def centre(self) -> tuple[float, float]:
        x, y, w, h = self.box
        return x + w / 2, y + h / 2

    @property
    def at_edge(self) -> bool:
        """Touching the frame's border: a person half out of the picture."""
        x, y, w, h = self.box
        return x < 0.03 or y < 0.03 or x + w > 0.97 or y + h > 0.97


def pack(sightings: Iterable[Sighting], *, limit: int = 160) -> list[list[float]]:
    """As stored on an event: the best `limit` by score, in time order."""
    best = sorted(sightings, key=lambda s: -s.score)[:limit]
    return [s.as_list() for s in sorted(best, key=lambda s: s.t)]


def unpack(raw: object) -> list[Sighting]:
    if not isinstance(raw, list):
        return []
    found = [Sighting.from_list(item) for item in raw]
    return sorted((s for s in found if s is not None), key=lambda s: s.t)


def stored_sightings(intent: Mapping[str, Any] | None) -> list[Sighting]:
    """The person sightings an event already has: its dense track when it was judged in
    windows, else the pre-filter's."""
    intent = intent or {}
    windows = intent.get("windows")
    if isinstance(windows, Mapping):
        track = unpack(windows.get("track"))
        if track:
            return track
    triage = intent.get("triage")
    observed = triage.get("observed") if isinstance(triage, Mapping) else None
    if isinstance(observed, Mapping):
        return unpack(observed.get("persons"))
    return []


def _detect(detector: ObjectDetector, paths: list[Path]) -> list[list[Sighting]]:
    out: list[list[Sighting]] = []
    for path in paths:
        out.append(
            [
                Sighting(0.0, d.score, d.box)
                for d in detector.detect(path.read_bytes())
                if d.kind == "person" and d.score >= MIN_SCORE
            ]
        )
    return out


async def person_track(
    video: Path,
    detector: ObjectDetector,
    *,
    every_s: float = TRACK_EVERY_S,
    max_frames: int = TRACK_MAX_FRAMES,
    work_dir: Path | None = None,
) -> list[Sighting]:
    """Every person the detector finds in a frame every `every_s` over the clip (fewer
    frames for a clip longer than `max_frames` × `every_s`)."""
    temp = Path(tempfile.mkdtemp(prefix="vidette-track-", dir=work_dir))
    try:
        paths = await sample_frames(
            video, temp, fps=1.0 / every_s, max_frames=max_frames, max_side=TRACK_SIDE
        )
        if not paths:
            return []
        found = await asyncio.to_thread(_detect, detector, paths)
    finally:
        with contextlib.suppress(OSError):
            shutil.rmtree(temp)
    step = every_s
    return [
        Sighting(round(index * step, 2), s.score, s.box)
        for index, frame in enumerate(found)
        for s in frame
    ]


def appearances(track: list[Sighting], *, gap_s: float = 4.0) -> list[list[Sighting]]:
    """The track split where nobody was seen for longer than `gap_s`: each piece is one
    appearance (someone walks in, stays, walks out)."""
    pieces: list[list[Sighting]] = []
    for sighting in sorted(track, key=lambda s: s.t):
        if pieces and sighting.t - pieces[-1][-1].t <= gap_s:
            pieces[-1].append(sighting)
        else:
            pieces.append([sighting])
    return pieces
