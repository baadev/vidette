"""A long clip with a person in it is judged in windows around the person (ADR-0022).

Four frames chosen where the picture changed cover a 20-second clip well. They do not
cover 80 seconds in which headlights, an animal and parked cars' lights keep the picture
changing: on a test deployment the judge saw a person far away at 47 s and at the
frame's edge at 64 s, and neither the person small and far away at 36 s nor the moment
at 72 s, at the bottom edge of the picture, that the clip was about. So for a clip
longer than `long_clip` the worker asks where the person is first:

    the detector, a frame a second ─▶ the person's track ─▶ appearances (split by gaps)
        ─▶ windows of at most `window` seconds, each ending at a sighting
        ─▶ the window with the LAST sighting first, then the ones where the person is
           small or at the edge, then where they stay longest in one place (lingering,
           working at something), at most `max_windows`
        ─▶ each window: `frames_per_call` frames spread over the person's sightings in
           it, the first and the last included; when the person is small or at the
           frame's edge, frame 1 shows the whole picture and the others are enlarged on
           the person (the part of the picture around everywhere they were in the window)

Each window is one judge call with the same policy. The frames chosen where the picture
changed (what the clip is judged on without windows) are judged as well, second: the
detector misses people half out of the picture, and nothing those frames show may be lost.
A window whose verdict wakes for anyone (a door, gate or window attempt; climbing;
touching the panel) decides at once and no more windows are judged; otherwise every
planned window is judged and the highest alerting level wins; when nothing alerts, the
usual frames' verdict stands. The track, the plan and each window's verdict are kept on
the event (`intent.windows`).

Budgets: the track costs the detector about a tenth of a second per frame on the CPU
(80 frames for an 80-second clip), and each extra window is one more model call; both
only for clips longer than `long_clip` in which the detector saw a person.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from pathlib import Path

from vidette.ffmpeg import MediaToolError, extract_frame
from vidette.streamhealth import StreamHealth
from vidette.triage.track import Sighting, appearances
from vidette.verdicts.base import FrameSet
from vidette.verdicts.frames import spread_indices

logger = logging.getLogger(__name__)

Box = tuple[float, float, float, float]
GAP_S = 4.0  # nobody seen this long: a new appearance
ZOOM_HEIGHT = 0.35  # a person shorter than this share of the picture is enlarged
EDGE = 0.03  # a box this close to the border is at the edge
MIN_CROP = 0.4  # an enlarged frame still shows at least this share of the picture
STILL_MOVE = 0.06  # a person moving less than this (of the picture) per second stays


@dataclass(frozen=True)
class Window:
    start: float  # seconds into the clip: the first sighting in it
    end: float  # the last sighting in it
    times: list[float]  # the frames to show
    crop: Box | None  # the part of the picture to enlarge, None: whole frames
    last: bool  # it holds the clip's last sighting of a person
    still_s: float  # how long the person stayed in one place in it
    sightings: list[Sighting] = field(default_factory=list, repr=False)
    # the last frame repeated, enlarged on the person: where they ended up is small or at
    # the edge of the picture (a hand at the bottom edge, half out of view)
    zoom_last: Box | None = None
    activity: bool = False  # the frames chosen where the picture changed, as without windows

    def record(self) -> dict[str, object]:
        out: dict[str, object] = {
            "from": round(self.start, 2),
            "to": round(self.end, 2),
            "frames": [round(t, 2) for t in self.times],
            "last": self.last,
        }
        if self.crop is not None:
            out["crop"] = [round(v, 4) for v in self.crop]
        if self.zoom_last is not None:
            out["zoom_last"] = [round(v, 4) for v in self.zoom_last]
        if self.activity:
            out["activity"] = True
        if self.still_s:
            out["still_s"] = round(self.still_s, 1)
        return out


def _union(boxes: list[Box]) -> Box:
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return x0, y0, x1 - x0, y1 - y0


def _at_edge(box: Box) -> bool:
    x, y, w, h = box
    return x < EDGE or y < EDGE or x + w > 1 - EDGE or y + h > 1 - EDGE


def crop_for(boxes: list[Box]) -> Box | None:
    """The part of the picture to enlarge so the person fills more of the judge's small
    image: everywhere they were in the window, with room around them, in the picture's
    own shape (so a frame costs the same tokens). None when they are already large and
    away from the edges, or the crop would be most of the picture anyway."""
    if not boxes:
        return None
    tallest = max(b[3] for b in boxes)
    if tallest >= ZOOM_HEIGHT and not any(_at_edge(b) for b in boxes):
        return None
    x, y, w, h = _union(boxes)
    size = max(MIN_CROP, w * 1.6, h * 1.5)
    if size >= 0.85:
        return None
    cx, cy = x + w / 2, y + h / 2
    left = min(max(0.0, cx - size / 2), 1.0 - size)
    top = min(max(0.0, cy - size / 2), 1.0 - size)
    return round(left, 4), round(top, 4), round(size, 4), round(size, 4)


def _still_s(piece: list[Sighting]) -> float:
    """How long the person stayed in one place: the time between consecutive sightings
    whose box centre moved less than STILL_MOVE of the picture per second."""
    total = 0.0
    for before, after in itertools.pairwise(piece):
        dt = after.t - before.t
        if dt <= 0:
            continue
        (ax, ay), (bx, by) = before.centre, after.centre
        if ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5 / dt < STILL_MOVE:
            total += dt
    return total


def _best_per_time(track: list[Sighting]) -> list[Sighting]:
    """One sighting per moment: the largest box (the person, not a far passer-by)."""
    best: dict[float, Sighting] = {}
    for sighting in track:
        held = best.get(sighting.t)
        if held is None or sighting.area > held.area:
            best[sighting.t] = sighting
    return [best[t] for t in sorted(best)]


def _pieces(appearance: list[Sighting], window_s: float) -> list[list[Sighting]]:
    """An appearance cut into pieces of at most `window_s`, from its end backwards: the
    last piece ends at the last sighting, wherever the cut falls."""
    pieces: list[list[Sighting]] = []
    rest = list(appearance)
    while rest:
        end = rest[-1].t
        piece = [s for s in rest if s.t >= end - window_s]
        rest = [s for s in rest if s.t < end - window_s]
        pieces.insert(0, piece)
    return pieces


def real_times(track: list[Sighting], gaps: list[tuple[float, float]]) -> list[Sighting]:
    """Sightings at moments the video really has. Inside a stall (`gaps`, from the clip's
    stream health) a frame sampled every second repeats the last frame before it: such a
    sighting is that frame, so it gets that frame's time (just before the stall), and a
    frame extracted at it shows what the detector saw, not the first frame after."""
    out: list[Sighting] = []
    for sighting in track:
        inside = next((a for a, b in gaps if a < sighting.t < b), None)
        if inside is None:
            out.append(sighting)
        else:
            out.append(Sighting(round(max(0.0, inside - 0.05), 2), sighting.score, sighting.box))
    return out


def activity_window(frames: FrameSet) -> Window:
    """The frames the clip would be judged on without windows, as one more window: the
    detector misses people half out of the picture, and these frames must never be lost
    by judging around the person instead."""
    times = [t for t in frames.times if t is not None]
    return Window(
        start=min(times, default=0.0), end=max(times, default=0.0),
        times=times, crop=None, last=False, still_s=0.0, activity=True,
    )  # fmt: skip


def plan(
    track: list[Sighting],
    *,
    frames: int,
    window_s: float = 20.0,
    max_windows: int = 3,
    gaps: list[tuple[float, float]] | None = None,
) -> list[Window]:
    """The windows to judge, in the order to judge them (see the module docstring)."""
    if frames <= 0 or not track:
        return []
    moments = _best_per_time(real_times(track, gaps or []))
    pieces = [
        piece for appearance in appearances(moments, gap_s=GAP_S)
        for piece in _pieces(appearance, window_s)
    ]  # fmt: skip
    if not pieces:
        return []
    last_t = moments[-1].t
    windows: list[Window] = []
    for piece in pieces:
        crop = crop_for([s.box for s in piece])
        final = piece[-1]
        zoom = None
        if crop is None and frames >= 3 and len(piece) >= 2:
            zoom = crop_for([final.box])
        count = frames - 1 if zoom is not None else frames
        picked = [piece[i] for i in spread_indices(len(piece), count)]
        times = [s.t for s in picked] + ([final.t] if zoom is not None else [])
        windows.append(
            Window(
                start=piece[0].t, end=final.t, times=times, crop=crop,
                last=final.t == last_t, still_s=_still_s(piece), sightings=piece,
                zoom_last=zoom,
            )
        )  # fmt: skip
    first = windows[-1]  # the last sighting: where the person ended up, what they did there
    # then where the person is small or at the edge (the usual frames cannot show them),
    # then where they stayed longest in one place
    others = sorted(
        windows[:-1],
        key=lambda w: (w.crop is not None, w.still_s, max(s.area for s in w.sightings)),
        reverse=True,
    )
    return [first, *others][: max(1, max_windows)]


async def window_frames(
    video: Path,
    window: Window,
    out_dir: Path,
    *,
    duration_s: float | None,
    health: StreamHealth | None,
    index: int,
    total: int,
    seen: tuple[float, float] | None,
) -> FrameSet:
    """The window's frames on disk (enlarged where `crop` says, frame 1 whole), with a
    line for the judge saying which part of the recording they cover."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    times: list[float | None] = []
    for number, at in enumerate(window.times, start=1):
        crop = window.crop if (window.crop is not None and number > 1) else None
        if window.zoom_last is not None and number == len(window.times):
            crop = window.zoom_last
        path = out_dir / f"w{index}-{number}.jpg"
        try:
            await extract_frame(video, at, path, crop=crop)
        except MediaToolError as exc:
            logger.info("window frame at %.1f s of %s not extracted: %s", at, video.name, exc)
            continue
        paths.append(path)
        times.append(round(at, 2))
    span = f"{window.start:.0f} to {window.end:.0f} s"
    note = f"These frames cover {span} of the recording, around the person"
    if total > 1:
        note += f" (part {index} of {total} looked at"
        if window.last:
            note += "; the last time anyone is in view"
        note += ")"
    if seen is not None:
        note += f"; a person is in view from {seen[0]:.0f} to {seen[1]:.0f} s in all"
    note += "."
    if window.crop is not None and len(paths) > 1:
        note += (
            f" Frame 1 is the whole picture; frames 2 to {len(paths)} are enlarged on the"
            " person."
        )
    elif window.zoom_last is not None and len(paths) == len(window.times) > 1:
        note += f" Frame {len(paths)} is frame {len(paths) - 1} enlarged on the person."
    return FrameSet(paths, times, "person", duration_s, health, note)
