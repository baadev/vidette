"""Clothing colours (ADR-0022): could two people minutes apart on neighbouring cameras be
the same one, when no face says so?

- **What is measured**: the colours of the clothes, from the detector's person box on a
  few frames of each clip: the torso and legs only (rows 30 to 95 % of the box's height,
  the middle 70 % of its width), so the head (face, hair, skin) is left out. A hue and
  saturation histogram of the coloured pixels and a brightness histogram of all of them,
  averaged over the frames.
- **What it is used for**: only to say "probably the same person" about two sightings
  that are minutes apart on cameras of one site, next to the timing. Never stored (only
  the resulting similarity goes into the link), never compared with anyone else's,
  never a reason to alert, raise a level or suspect anyone, never shown.
- **What it cannot do**: tell two people in dark clothes apart, or see colours in grey
  infrared at night (then only brightness counts, and the threshold is stricter). It is
  a weak cue by design: a face match always wins, and without either the link says only
  "at the same time".

Needs OpenCV and NumPy (`vidette[people]`, like the detector); without them there is no
clothing cue and links rest on faces and timing.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vidette.ffmpeg import MediaToolError, extract_frame
from vidette.triage.track import Sighting

HUE_BINS, SAT_BINS, VALUE_BINS = 12, 3, 6
MIN_BOX_PX = 48  # a box shorter than this on the frame says little about clothes
COLOURED = 0.18  # saturation (0..1) from which a pixel's hue counts
GREY_SHARE = 0.85  # a crop this grey is night infrared: hue says nothing
FRAMES = 3


@dataclass(frozen=True)
class Look:
    hue: tuple[float, ...]  # HUE_BINS × SAT_BINS, normalised (empty when grey)
    value: tuple[float, ...]  # VALUE_BINS, normalised
    grey: bool  # mostly uncoloured: night infrared, or grey and black clothes
    frames: int


def _bhattacharyya(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return float(sum((x * y) ** 0.5 for x, y in zip(a, b, strict=True)))


def similarity(a: Look, b: Look) -> float:
    """0 (nothing alike) to 1 (the same colours). Brightness always counts; hue only
    when both are coloured."""
    value = _bhattacharyya(a.value, b.value)
    if a.grey or b.grey or not a.hue or not b.hue:
        return round(value, 3)
    return round(0.6 * _bhattacharyya(a.hue, b.hue) + 0.4 * value, 3)


def _crop_histograms(image: Any, box: tuple[float, float, float, float]) -> Any:
    import cv2
    import numpy as np

    height, width = image.shape[:2]
    x, y, w, h = box
    if h * height < MIN_BOX_PX:
        return None
    top, bottom = y + 0.30 * h, y + 0.95 * h
    left, right = x + 0.15 * w, x + 0.85 * w
    crop = image[
        max(0, int(top * height)) : min(height, int(bottom * height)),
        max(0, int(left * width)) : min(width, int(right * width)),
    ]
    if crop.size == 0:
        return None
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(np.float32)
    hue, sat, val = hsv[:, 0] / 180.0, hsv[:, 1] / 255.0, hsv[:, 2] / 255.0
    coloured = sat >= COLOURED
    value_hist, _ = np.histogram(val, bins=VALUE_BINS, range=(0.0, 1.0))
    hue_hist, _, _ = np.histogram2d(
        hue[coloured], sat[coloured], bins=(HUE_BINS, SAT_BINS), range=((0, 1), (COLOURED, 1))
    )
    return hue_hist.reshape(-1), value_hist, float(coloured.mean())


def _look(paths: list[tuple[Path, tuple[float, float, float, float]]]) -> Look | None:
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    hue_sum = np.zeros(HUE_BINS * SAT_BINS)
    value_sum = np.zeros(VALUE_BINS)
    coloured: list[float] = []
    used = 0
    for path, box in paths:
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            continue
        found = _crop_histograms(image, box)
        if found is None:
            continue
        hue_hist, value_hist, share = found
        if hue_hist.sum() > 0:
            hue_sum += hue_hist / hue_hist.sum()
        value_sum += value_hist / max(1, value_hist.sum())
        coloured.append(share)
        used += 1
    if not used:
        return None
    grey = sum(coloured) / len(coloured) < 1 - GREY_SHARE
    hue = tuple(float(v) for v in hue_sum / hue_sum.sum()) if hue_sum.sum() > 0 else ()
    return Look(
        hue=hue, value=tuple(float(v) for v in value_sum / value_sum.sum()), grey=grey,
        frames=used,
    )  # fmt: skip


def pick(sightings: list[Sighting], count: int = FRAMES) -> list[Sighting]:
    """The largest sightings, at least a second apart (the clearest views of the clothes)."""
    chosen: list[Sighting] = []
    for sighting in sorted(sightings, key=lambda s: -s.area):
        if any(abs(sighting.t - c.t) < 1.0 for c in chosen):
            continue
        chosen.append(sighting)
        if len(chosen) >= count:
            break
    return chosen


async def look_of(
    video: Path, sightings: list[Sighting], *, work_dir: Path | None = None
) -> Look | None:
    """The clothes' colours of the person in `sightings` (one clip); None when no frame
    gave a usable crop or OpenCV is not installed."""
    chosen = pick(sightings)
    if not chosen:
        return None
    temp = Path(tempfile.mkdtemp(prefix="vidette-look-", dir=work_dir))
    try:
        frames: list[tuple[Path, tuple[float, float, float, float]]] = []
        for index, sighting in enumerate(chosen):
            try:
                path = await extract_frame(video, sighting.t, temp / f"{index}.jpg")
            except (MediaToolError, OSError):
                continue
            frames.append((path, sighting.box))
        return await asyncio.to_thread(_look, frames) if frames else None
    finally:
        with contextlib.suppress(OSError):
            shutil.rmtree(temp)
