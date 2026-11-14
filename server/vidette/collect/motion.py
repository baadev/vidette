"""Motion gate for `vidette collect --continuous`: frame differences, masks, smoothing.

ffmpeg does the pixel work. It decodes the stream a second time, keeps a few frames per
second, shrinks them to 256×144 grey and emits `|frame − previous frame|` (the `tblend`
difference filter). This module only counts: how many pixels of each difference image
changed by more than `pixel_threshold`, outside the masked regions, as a fraction of the
unmasked area. No image library, no numpy: `bytes.translate` and `bytes.count` do the
counting in C, about 0.1 ms per frame.

From that changed area per frame to motion events:

- **Area band.** A frame counts as active when its changed area is between `min_area`
  (default 0.1 % of the frame: a person far down the street still passes) and `max_area`
  (default 40 %: more than that at once is light, not motion: a cloud, headlights
  sweeping the wall, the camera switching to infrared at dusk).
- **Duration.** An event opens only after `min_motion_s` of active frames (default 1 s)
  without a quiet gap longer than `gap_s`. A leaf flicking once or a single noisy frame
  never opens one.
- **Gap.** An open event ends after `gap_s` (default 2 s) without an active frame, so a
  person pausing at the gate stays one event.
- **Length cap.** An event longer than `max_event_s` is closed there (marked `capped`),
  and the detector then waits for `gap_s` of quiet before it opens another. A tree in a
  storm produces one capped clip, not one every two minutes.
- **Masks.** Regions as fractions of the frame the camera sends (before `--rotate`). The
  default masks the top-right strip where Eufy (and most cameras) burn in the date and
  time: the clock changes every second and would otherwise be "motion" forever.

The thresholds are deliberately permissive: a dataset collector would rather keep a few
boring clips (the VLM drafts and the person labelling discard them) than miss a visitor.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum

ANALYSIS_WIDTH, ANALYSIS_HEIGHT = 256, 144


class MaskError(ValueError):
    pass


@dataclass(frozen=True)
class Region:
    """A rectangle as fractions (0..1) of the frame as the camera sends it."""

    x: float
    y: float
    w: float
    h: float

    @classmethod
    def parse(cls, text: str) -> Region:
        """`x,y,w,h` as fractions (`0.6,0,0.4,0.1`) or percentages (`60%,0,40%,10%`)."""
        parts = [part.strip() for part in text.split(",")]
        if len(parts) != 4:
            raise MaskError(
                f"mask '{text}' must be x,y,w,h (fractions of the frame, e.g. "
                "0.6,0,0.4,0.1 for the top-right strip)"
            )
        values: list[float] = []
        for part in parts:
            try:
                value = float(part[:-1]) / 100 if part.endswith("%") else float(part)
            except ValueError:
                raise MaskError(f"mask '{text}': '{part}' is not a number") from None
            values.append(value)
        x, y, w, h = values
        if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1):
            raise MaskError(f"mask '{text}': x and y must be in [0, 1), w and h in (0, 1]")
        if x + w > 1.0001 or y + h > 1.0001:
            raise MaskError(f"mask '{text}' reaches outside the frame (x + w and y + h ≤ 1)")
        return cls(x, y, min(w, 1 - x), min(h, 1 - y))

    def pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        """(x0, y0, x1, y1) in a `width`×`height` image, rounded outwards."""
        x0, y0 = math.floor(self.x * width), math.floor(self.y * height)
        x1 = min(width, math.ceil((self.x + self.w) * width))
        y1 = min(height, math.ceil((self.y + self.h) * height))
        return x0, y0, x1, y1

    def __str__(self) -> str:
        return f"{self.x:g},{self.y:g},{self.w:g},{self.h:g}"


# The date and time overlay: Eufy burns it into the top-right corner of the picture as
# the camera sends it (also when the camera hangs on its side: the overlay stays
# horizontal, the scene turns).
OVERLAY_MASK = Region(0.6, 0.0, 0.4, 0.1)


def parse_fraction(text: str) -> float:
    """`0.5%` → 0.005, `0.005` → 0.005, `40%` → 0.4."""
    raw = text.strip()
    try:
        value = float(raw[:-1]) / 100 if raw.endswith("%") else float(raw)
    except ValueError:
        raise ValueError(f"'{text}' is not a fraction; use e.g. 0.1% or 0.001") from None
    if not 0 < value <= 1:
        raise ValueError(f"'{text}' must be more than 0 and at most 100%")
    return value


@dataclass(frozen=True)
class MotionSettings:
    fps: float = 4.0
    width: int = ANALYSIS_WIDTH
    height: int = ANALYSIS_HEIGHT
    pixel_threshold: int = 25  # grey levels (of 255) a pixel must change by
    min_area: float = 0.001  # of the unmasked frame
    max_area: float = 0.4  # above this a frame is a light change, not motion
    min_motion_s: float = 1.0
    gap_s: float = 2.0
    masks: tuple[Region, ...] = (OVERLAY_MASK,)

    def __post_init__(self) -> None:
        if not 1 <= self.pixel_threshold <= 254:
            raise ValueError("--motion-threshold must be between 1 and 254 grey levels")
        if not 0 < self.min_area < self.max_area <= 1:
            raise ValueError("--motion-area must be smaller than --motion-max-area")
        if not 0.5 <= self.fps <= 15:
            raise ValueError("--motion-fps must be between 0.5 and 15")

    @property
    def frame_bytes(self) -> int:
        return self.width * self.height

    def filter_graph(self) -> str:
        """ffmpeg's half: fps, shrink, grey, difference to the previous frame."""
        return (
            f"fps={self.fps:g},scale={self.width}:{self.height}:flags=area,format=gray,"
            "tblend=all_mode=difference"
        )


class FrameScorer:
    """Changed area of one difference image, ignoring masked pixels."""

    def __init__(self, settings: MotionSettings) -> None:
        self.width, self.height = settings.width, settings.height
        self._table = bytes(1 if level >= settings.pixel_threshold else 0 for level in range(256))
        masked = bytearray(self.width * self.height)
        for region in settings.masks:
            x0, y0, x1, y1 = region.pixels(self.width, self.height)
            for row in range(y0, y1):
                masked[row * self.width + x0 : row * self.width + x1] = b"\x01" * (x1 - x0)
        self.masked_pixels = masked.count(1)
        self.unmasked_pixels = len(masked) - self.masked_pixels
        if self.unmasked_pixels < len(masked) // 10:
            raise MaskError("the masks cover more than 90 % of the frame; nothing left to watch")
        self._spans = _runs(masked)

    def area(self, diff: bytes) -> float:
        if len(diff) != self.width * self.height:
            raise ValueError(f"expected {self.width}×{self.height} grey bytes, got {len(diff)}")
        binary = diff.translate(self._table)
        changed = binary.count(1) - sum(binary.count(1, start, end) for start, end in self._spans)
        return changed / self.unmasked_pixels


def _runs(mask: bytearray) -> list[tuple[int, int]]:
    """[start, end) spans of consecutive 1s."""
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(mask):
        if value and start is None:
            start = index
        elif not value and start is not None:
            spans.append((start, index))
            start = None
    if start is not None:
        spans.append((start, len(mask)))
    return spans


@dataclass(frozen=True)
class MotionEvent:
    start: float  # wall clock (epoch s) of the first active frame
    end: float  # wall clock of the last active frame
    active_s: float  # time covered by active frames
    peak_area: float
    capped: bool  # closed at the length cap while motion went on

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end - self.start)


class _Phase(StrEnum):
    idle = "idle"
    candidate = "candidate"  # active frames seen, not yet min_motion_s of them
    event = "event"
    saturated = "saturated"  # after a capped event: wait for quiet


@dataclass
class _Track:
    start: float
    last_active: float
    active_s: float = 0.0
    peak_area: float = 0.0


@dataclass
class MotionDetector:
    """Turns per-frame changed areas into events. Pure: feed it areas and times."""

    settings: MotionSettings
    max_event_s: float
    frames: int = 0
    active_frames: int = 0
    light_frames: int = 0
    _phase: _Phase = _Phase.idle
    _track: _Track | None = None
    _last_active: float = field(default=0.0)

    @property
    def busy(self) -> bool:
        """An event is open (or about to be): its clip is not decided yet."""
        return self._phase in (_Phase.candidate, _Phase.event)

    @property
    def open_since(self) -> float | None:
        return self._track.start if self._track is not None else None

    def feed(self, area: float, at: float) -> MotionEvent | None:
        """One difference frame's changed area at wall time `at`; a closed event or None."""
        self.frames += 1
        settings = self.settings
        if area > settings.max_area:
            self.light_frames += 1
            active = False
        else:
            active = area >= settings.min_area
        if active:
            self.active_frames += 1
        step = 1 / settings.fps

        if self._phase is _Phase.saturated:
            if active:
                self._last_active = at
            elif at - self._last_active >= settings.gap_s:
                self._phase = _Phase.idle
            return None

        if self._phase is _Phase.idle:
            if active:
                self._track = _Track(start=at, last_active=at, active_s=step, peak_area=area)
                self._phase = _Phase.candidate
                self._promote()
            return None

        track = self._track
        assert track is not None
        if active:
            track.last_active = at
            track.active_s += step
            track.peak_area = max(track.peak_area, area)
            if self._phase is _Phase.candidate:
                self._promote()
                return None
            if at - track.start >= self.max_event_s:
                self._phase, self._last_active = _Phase.saturated, at
                return self._close(capped=True)
            return None
        if at - track.last_active >= settings.gap_s:
            if self._phase is _Phase.candidate:
                self._phase, self._track = _Phase.idle, None  # a blip: never an event
                return None
            self._phase = _Phase.idle
            return self._close(capped=False)
        if self._phase is _Phase.event and at - track.start >= self.max_event_s:
            self._phase, self._last_active = _Phase.saturated, track.last_active
            return self._close(capped=True)
        return None

    def _promote(self) -> None:
        """A candidate with `min_motion_s` of active frames becomes an event."""
        track = self._track
        assert track is not None
        if track.active_s >= self.settings.min_motion_s - 1e-9:
            self._phase = _Phase.event

    def _close(self, *, capped: bool) -> MotionEvent:
        track = self._track
        assert track is not None
        self._track = None
        return MotionEvent(
            start=track.start,
            end=track.last_active,
            active_s=round(track.active_s, 2),
            peak_area=round(track.peak_area, 4),
            capped=capped,
        )

    def flush(self) -> MotionEvent | None:
        """The stream ended: close an open event (a candidate is dropped)."""
        phase, self._phase = self._phase, _Phase.idle
        if phase is _Phase.event:
            return self._close(capped=False)
        self._track = None
        return None
