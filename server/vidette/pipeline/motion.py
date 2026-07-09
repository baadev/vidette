"""Tier 0 motion gate — frame differencing against a slowly adapting background.

Pure numpy, no cv2 (ADR-scale dependency discipline): grayscale by channel mean, an
exponential-moving-average background, and an absolute-difference threshold. The output is
``MotionRegion`` s from ``vidette.pipeline.base`` — normalized boxes plus the changed-pixel
fraction as the score — which is exactly what wakes Tier 1.

Day/night damping: an IR cut-over or lights-on flips the whole frame at once. When the
global mean luma jumps by more than 40 between consecutive frames, the background is reset
to the new frame and *nothing* is reported for that frame — one quiet frame beats a
full-screen false positive. Normal reporting resumes on the next frame.

Region extraction v1 (documented simplification): the change mask is pooled over an 8×8
grid and one bounding box over all active cells is returned. Per-connected-component
regions are a later refinement; Tier 1 crops generously anyway.

Note on the signature: ``base.MotionGate`` sketches ``process(at: datetime, ...)``; the
Tier 0 runner contract uses epoch-float timestamps end to end, so this gate takes
``ts: float``. The seam is otherwise identical (frame in, regions out).
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from vidette.pipeline.base import BBox, MotionRegion

_LUMA_JUMP_RESET = 40.0  # mean-luma delta between consecutive frames that means "new scene"
_GRID = 8  # coarse pooling grid for region extraction


class FrameDiffGate:
    """Per-camera motion gate; feed it every decoded substream frame, in order.

    Args:
        threshold: minimum changed-pixel fraction (0..1) that counts as motion.
        sensitivity: per-pixel absolute gray delta (0..255) that counts as "changed".
        warmup_frames: initial frames reported as quiet while the background settles.
        decay: EMA weight of the newest frame in the background (0..1, small = slow).
    """

    def __init__(
        self,
        *,
        threshold: float = 0.012,
        sensitivity: int = 25,
        warmup_frames: int = 5,
        decay: float = 0.05,
    ) -> None:
        self._threshold = threshold
        self._sensitivity = sensitivity
        self._warmup_frames = warmup_frames
        self._decay = decay
        self._background: npt.NDArray[np.float32] | None = None
        self._last_luma: float | None = None
        self._frames_seen = 0

    def process(self, ts: float, frame_bgr: npt.NDArray[np.uint8]) -> list[MotionRegion]:
        """Frame in, motion regions out. Empty list means "scene quiet" — Tier 1 sleeps."""
        gray = np.asarray(frame_bgr, dtype=np.float32).mean(axis=2, dtype=np.float32)
        mean_luma = float(gray.mean())
        self._frames_seen += 1

        if self._background is None:
            self._background = gray
            self._last_luma = mean_luma
            return []

        last_luma = self._last_luma if self._last_luma is not None else mean_luma
        self._last_luma = mean_luma
        if abs(mean_luma - last_luma) > _LUMA_JUMP_RESET:
            # Day/night transition: adopt the new scene, report nothing this frame.
            self._background = gray
            return []

        diff = np.abs(gray - self._background)
        mask = diff > self._sensitivity
        self._background = (1.0 - self._decay) * self._background + self._decay * gray

        if self._frames_seen <= self._warmup_frames:
            return []
        changed = float(mask.mean())
        if changed < self._threshold:
            return []
        return [MotionRegion(bbox=self._active_bbox(mask), score=min(1.0, changed))]

    def _active_bbox(self, mask: npt.NDArray[np.bool_]) -> BBox:
        """One normalized box over all active cells of an 8×8 pooling grid (v1)."""
        h, w = mask.shape
        rows: list[int] = []
        cols: list[int] = []
        for r in range(_GRID):
            y0, y1 = r * h // _GRID, (r + 1) * h // _GRID
            for c in range(_GRID):
                x0, x1 = c * w // _GRID, (c + 1) * w // _GRID
                cell = mask[y0:y1, x0:x1]
                if cell.size > 0 and float(cell.mean()) > self._threshold:
                    rows.append(r)
                    cols.append(c)
        if not rows:
            # Change is spread too thin for any one cell — report the whole frame.
            return BBox(x=0.0, y=0.0, w=1.0, h=1.0)
        x_min = min(cols) * w // _GRID
        x_max = (max(cols) + 1) * w // _GRID
        y_min = min(rows) * h // _GRID
        y_max = (max(rows) + 1) * h // _GRID
        return BBox(
            x=x_min / w,
            y=y_min / h,
            w=(x_max - x_min) / w,
            h=(y_max - y_min) / h,
        )
