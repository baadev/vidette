"""Pure, fast tests for the Tier 0 motion gate — no ffmpeg, no IO."""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from vidette.pipeline.motion import FrameDiffGate

H, W = 96, 128  # small analysis raster; 8x8 grid cells are 12x16 px


def _frame(fill: int = 0) -> npt.NDArray[np.uint8]:
    return np.full((H, W, 3), fill, dtype=np.uint8)


def _square(
    x0: int, y0: int, *, size: int = 24, bg: int = 0, value: int = 255
) -> npt.NDArray[np.uint8]:
    frame = _frame(bg)
    frame[y0 : y0 + size, x0 : x0 + size, :] = value
    return frame


def test_warmup_returns_no_regions_even_with_motion() -> None:
    gate = FrameDiffGate(warmup_frames=5)
    for i in range(5):
        assert gate.process(float(i), _square(8 * i, 8 * i)) == []


def test_static_scene_stays_quiet_after_warmup() -> None:
    gate = FrameDiffGate(warmup_frames=3)
    for i in range(15):
        assert gate.process(float(i), _frame(60)) == []


def test_moving_square_produces_covering_region() -> None:
    gate = FrameDiffGate(warmup_frames=3)
    for i in range(3):
        assert gate.process(float(i), _frame()) == []

    regions = gate.process(3.0, _square(32, 24))
    assert len(regions) == 1
    region = regions[0]
    assert region.score > 0.0
    box = region.bbox
    assert 0.0 <= box.x <= 1.0 and 0.0 <= box.y <= 1.0
    assert 0.0 < box.w <= 1.0 and 0.0 < box.h <= 1.0
    # The region covers the square (normalized square: x 0.25..0.4375, y 0.25..0.5)...
    assert box.x <= 32 / W and box.x + box.w >= (32 + 24) / W
    assert box.y <= 24 / H and box.y + box.h >= (24 + 24) / H
    # ...without ballooning to the whole frame.
    assert box.w <= 0.5 and box.h <= 0.5
    # Score is the changed-pixel fraction: a 24x24 square on 96x128 is ~4.7 %.
    assert 0.0 < region.score < 0.5

    # It keeps firing while the square keeps moving (background barely adapted).
    assert gate.process(4.0, _square(64, 48)) != []


def test_luma_jump_resets_background_then_recovers() -> None:
    gate = FrameDiffGate(warmup_frames=2)
    assert gate.process(0.0, _frame(200)) == []  # warmup
    assert gate.process(1.0, _frame(200)) == []  # warmup
    assert gate.process(2.0, _frame(200)) == []  # past warmup, static → quiet
    # Night flip: without damping this full-frame change would be a 100 % motion region.
    assert gate.process(3.0, _frame(20)) == []
    # The background was reset to the new scene, so the next static frame is quiet...
    assert gate.process(4.0, _frame(20)) == []
    # ...and real motion on the new scene is detected again (mean-luma delta stays small).
    regions = gate.process(5.0, _square(32, 24, bg=20, value=255))
    assert len(regions) == 1
    assert regions[0].score > 0.0
