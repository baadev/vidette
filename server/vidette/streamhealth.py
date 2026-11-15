"""Stream health: did a clip's video arrive whole and evenly?

Measured from the clip's packet times alone (`ffprobe`, no decoding). A camera on a weak
link can stop delivering frames for seconds and then deliver the backlog in a burst; a
recording stamped on arrival then shows a frozen picture followed by fast-forward, and a
recording that loses frames shows a jump. Either way a person walking past can look like
a person standing still, so the judge is told where the video paused, and the digest and
the alert say "the recording was interrupted".

    frames          video packets (one per frame)
    span_s          first to last frame
    fps             frames per second over the span (what arrived)
    nominal_fps     what the stream says it is
    stalls          pauses between two frames longer than STALL_S
    missing_s       the time those pauses cover
    longest_stall_s the longest one, and where it starts (`longest_at_s`)
    burst_share     share of frame intervals shorter than 40 % of the nominal one:
                    frames that arrived in a heap after a pause
    gaps            the longest pauses of at least GAP_S, [start, end] in clip seconds

`interrupted` is the one bit people see: a pause of at least 2 s, or pauses covering at
least a tenth of the clip.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from vidette.ffmpeg import packet_times

STALL_S = 0.5  # a pause longer than this between two frames is a stall
GAP_S = 1.0  # stalls at least this long are listed (and told to the judge)
MAX_GAPS = 12
INTERRUPTED_STALL_S = 2.0
INTERRUPTED_SHARE = 0.10
BURST_FRACTION = 0.4


class StreamHealth(BaseModel):
    version: Literal[1] = 1
    frames: int = Field(ge=0)
    span_s: float = Field(ge=0)
    fps: float | None = None
    nominal_fps: float | None = None
    stalls: int = Field(default=0, ge=0)
    missing_s: float = Field(default=0.0, ge=0)
    longest_stall_s: float = Field(default=0.0, ge=0)
    longest_at_s: float | None = None
    burst_share: float = Field(default=0.0, ge=0, le=1)
    gaps: list[tuple[float, float]] = Field(default_factory=list)

    @property
    def interrupted(self) -> bool:
        if self.longest_stall_s >= INTERRUPTED_STALL_S:
            return True
        return self.span_s > 0 and self.missing_s / self.span_s >= INTERRUPTED_SHARE

    def gaps_between(self, start_s: float, end_s: float) -> list[tuple[float, float]]:
        """Listed pauses that overlap [start_s, end_s] (clip seconds)."""
        return [(a, b) for a, b in self.gaps if b > start_s and a < end_s]


def measure(times: list[float], nominal_fps: float | None = None) -> StreamHealth:
    """Health from packet times (seconds, any origin; sorted here)."""
    ordered = sorted(times)
    if len(ordered) < 2:
        return StreamHealth(frames=len(ordered), span_s=0.0, nominal_fps=nominal_fps)
    origin = ordered[0]
    span = ordered[-1] - origin
    steps = [(a - origin, b - a) for a, b in itertools.pairwise(ordered)]
    fps = (len(ordered) - 1) / span if span > 0 else None
    stalls = [(at, step) for at, step in steps if step > STALL_S]
    longest_at, longest = max(stalls, key=lambda s: s[1], default=(None, 0.0))
    reference = nominal_fps or fps
    burst = 0.0
    if reference:
        short = BURST_FRACTION / reference
        burst = sum(1 for _at, step in steps if step < short) / len(steps)
    listed = sorted((s for s in stalls if s[1] >= GAP_S), key=lambda s: s[1], reverse=True)
    gaps = sorted((round(at, 2), round(at + step, 2)) for at, step in listed[:MAX_GAPS])
    return StreamHealth(
        frames=len(ordered),
        span_s=round(span, 2),
        fps=None if fps is None else round(fps, 2),
        nominal_fps=nominal_fps,
        stalls=len(stalls),
        missing_s=round(sum(step for _at, step in stalls), 2),
        longest_stall_s=round(longest, 2),
        longest_at_s=None if longest_at is None else round(longest_at, 2),
        burst_share=round(burst, 3),
        gaps=gaps,
    )


async def measure_file(path: Path, nominal_fps: float | None = None) -> StreamHealth:
    """Health of a media file; MediaToolError when ffprobe is missing or fails."""
    return measure(await packet_times(path), nominal_fps)
