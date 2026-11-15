"""Find faces and turn them into embeddings: OpenCV's YuNet and SFace, on this machine.

`FaceEngine` is the boundary; `OpenCvFaceEngine` is the only implementation that needs
OpenCV (`pip install 'vidette[people]'`), and tests use a fake. Every face found gets a
quality verdict before anyone compares it with a person:

- readable: detector score >= `min_score`, face at least `min_face_px` tall in the
  frame, and turned to the camera enough (eye distance / face width >= `min_frontal`);
  only these are embedded and compared;
- unclear: a face, but too small, too uncertain, turned away, or with a weak embedding
  (SFace's raw feature norm below `min_quality`: blurred faces and faces seen from
  steeply above score low). Unclear faces are never called "unknown": a 20-pixel face at
  the gate says nothing about who it is.

YuNet is trained on faces of about 10 to 300 px, so each frame is searched at full size
and, when it is large, at half size too (a face filling the lens at arm's length); the
two sets of boxes are merged.

Every face also gets the signals that choose the best moment of a person in a clip
(ADR-0016): how frontal it is, SFace's feature norm, and the sharpness of its aligned
crop (variance of the Laplacian). Unclear faces get a crop and an embedding too, marked
by `readable=False`: the crop is something a person can look at and the embedding a hint
for suggesting merges, never a match.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from vidette.people.vectors import Vector, normalize

ENGINE_NAME = "yunet-2023mar+sface-2021dec"
CROP_SIDE = 112  # SFace's aligned input


class EngineUnavailableError(Exception):
    """OpenCV is not installed or the models cannot be loaded; the message says how."""


@dataclass(frozen=True)
class FaceLimits:
    min_face_px: int = 40  # smaller faces are counted as unclear, never compared
    min_score: float = 0.8  # detector confidence for a readable face
    detect_score: float = 0.7  # below this a "face" is ignored (YuNet's false positives)
    min_frontal: float = 0.25  # eye distance / face width; profiles embed poorly
    # SFace's raw feature norm as a rough quality proxy. On real footage, crops of a
    # scalp seen from above scored 5.7 to 7.1, clear faces 8.7 to 12.9, and 95 % of
    # reference faces from phone screenshots above 6.9. A heuristic, not a measured rate.
    min_quality: float = 7.0


@dataclass(frozen=True)
class DetectedFace:
    box: tuple[int, int, int, int]  # x, y, width, height in the image as given
    score: float  # detector confidence
    face_px: int  # face height in the image as given
    readable: bool
    unclear_reason: str | None = None  # "too small", "turned away", "uncertain"
    embedding: Vector | None = None  # readable faces; unclear ones too (a hint, never a match)
    crop: bytes | None = None  # aligned 112×112 JPEG
    frontal: float = 0.0  # eye distance / face width (about 0.45 for a face looking ahead)
    quality: float = 0.0  # SFace's raw feature norm
    sharpness: float = 0.0  # variance of the Laplacian of the aligned crop, grey levels²


SHARP_REF = 150.0  # a crop this sharp or sharper counts as fully sharp in `moment_score`


def moment_score(face: DetectedFace) -> float:
    """How good this face is as *the* picture of a person, 0 to about 1: detector
    confidence × sharpness × size × frontalness. Readable faces always rank above unclear
    ones (+1). Used to pick the crop, the embedding and the preview of a person."""
    sharp = min(1.0, face.sharpness / SHARP_REF)
    size = min(1.0, face.face_px / 112)
    frontal = min(1.0, face.frontal / 0.45)
    return face.score * sharp * size * frontal + (1.0 if face.readable else 0.0)


class FaceEngine(Protocol):
    @property
    def name(self) -> str: ...

    def faces(self, image: bytes, *, max_side: int | None = None) -> list[DetectedFace]:
        """Faces in an encoded image (JPEG/PNG/WebP); `max_side` shrinks it first."""
        ...

    def embed_crop(self, crop: bytes) -> Vector:
        """The embedding of an aligned crop this engine produced earlier."""
        ...


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


class OpenCvFaceEngine:
    """YuNet + SFace through `cv2.FaceDetectorYN` / `cv2.FaceRecognizerSF`.

    Not thread-safe internally (OpenCV's DNN nets are not), so calls are serialised with
    a lock; callers run it in a worker thread to keep the event loop free."""

    def __init__(self, detector: Path, recognizer: Path, limits: FaceLimits | None = None):
        try:
            import cv2
            import numpy
        except ImportError as exc:
            raise EngineUnavailableError(
                "face recognition needs OpenCV: pip install 'vidette[people]' "
                "(in a source checkout: uv sync --extra people)"
            ) from exc
        self._cv2: Any = cv2
        self._np: Any = numpy
        self.limits = limits or FaceLimits()
        try:
            self._detector: Any = cv2.FaceDetectorYN.create(
                str(detector), "", (320, 320), self.limits.detect_score, 0.3, 5000
            )
            self._recognizer: Any = cv2.FaceRecognizerSF.create(str(recognizer), "")
        except cv2.error as exc:
            raise EngineUnavailableError(f"OpenCV cannot load the face models: {exc}") from exc
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return ENGINE_NAME

    def _decode(self, image: bytes) -> Any:
        cv2, np = self._cv2, self._np
        array = np.frombuffer(image, dtype=np.uint8)
        img = cv2.imdecode(array, cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("not an image OpenCV can read (JPEG, PNG or WebP expected)")
        return img

    def _detect(self, img: Any) -> list[tuple[Any, Any]]:
        """(row in `img` coordinates, the image the row belongs to) per face, merged
        over the full size and a half-size pass."""
        cv2 = self._cv2
        height, width = img.shape[:2]
        passes: list[tuple[Any, float]] = [(img, 1.0)]
        if min(height, width) >= 480:
            half = cv2.resize(img, (width // 2, height // 2), interpolation=cv2.INTER_AREA)
            passes.append((half, 0.5))
        found: list[tuple[float, tuple[int, int, int, int], Any, Any]] = []
        for picture, scale in passes:
            h, w = picture.shape[:2]
            self._detector.setInputSize((w, h))
            _ok, rows = self._detector.detect(picture)
            if rows is None:
                continue
            for row in rows:
                x, y, bw, bh = (float(v) / scale for v in row[:4])
                box = (round(x), round(y), round(bw), round(bh))
                found.append((float(row[14]), box, row, picture))
        found.sort(key=lambda item: -item[0])
        kept: list[tuple[float, tuple[int, int, int, int], Any, Any]] = []
        for item in found:
            if all(_iou(item[1], other[1]) < 0.4 for other in kept):
                kept.append(item)
        return [(row, picture) for _score, _box, row, picture in kept]

    def faces(self, image: bytes, *, max_side: int | None = None) -> list[DetectedFace]:
        cv2 = self._cv2
        img = self._decode(image)
        scale = 1.0
        if max_side is not None and max(img.shape[:2]) > max_side:
            scale = max_side / max(img.shape[:2])
            img = cv2.resize(
                img,
                (round(img.shape[1] * scale), round(img.shape[0] * scale)),
                interpolation=cv2.INTER_AREA,
            )
        limits = self.limits
        out: list[DetectedFace] = []
        with self._lock:
            detections = self._detect(img)
            for row, picture in detections:
                ratio = img.shape[1] / picture.shape[1]  # 1.0 or 2.0
                score = float(row[14])
                box_w, box_h = float(row[2]) * ratio, float(row[3]) * ratio
                box = (
                    round(float(row[0]) * ratio / scale),
                    round(float(row[1]) * ratio / scale),
                    round(box_w / scale),
                    round(box_h / scale),
                )
                face_px = round(box_h / scale)
                eye = math.hypot(float(row[4]) - float(row[6]), float(row[5]) - float(row[7]))
                frontal = eye / float(row[2]) if float(row[2]) > 0 else 0.0
                aligned = self._recognizer.alignCrop(picture, row)
                feature = self._recognizer.feature(aligned)
                quality = float(self._np.linalg.norm(feature))
                grey = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
                sharpness = float(cv2.Laplacian(grey, cv2.CV_64F).var())
                reason = None
                if face_px < limits.min_face_px:
                    reason = "too small"
                elif score < limits.min_score:
                    reason = "uncertain"
                elif frontal < limits.min_frontal:
                    reason = "turned away"
                elif quality < limits.min_quality:
                    reason = "low quality"
                ok, encoded = cv2.imencode(".jpg", aligned, [cv2.IMWRITE_JPEG_QUALITY, 95])
                out.append(
                    DetectedFace(
                        box=box,
                        score=score,
                        face_px=face_px,
                        readable=reason is None,
                        unclear_reason=reason,
                        embedding=normalize(float(v) for v in feature.flatten()),
                        crop=bytes(encoded.tobytes()) if ok else None,
                        frontal=round(frontal, 3),
                        quality=round(quality, 2),
                        sharpness=round(sharpness, 1),
                    )
                )
        return out

    def embed_crop(self, crop: bytes) -> Vector:
        cv2 = self._cv2
        img = self._decode(crop)
        if img.shape[0] != CROP_SIDE or img.shape[1] != CROP_SIDE:
            img = cv2.resize(img, (CROP_SIDE, CROP_SIDE), interpolation=cv2.INTER_AREA)
        with self._lock:
            feature = self._recognizer.feature(img)
        return normalize(float(v) for v in feature.flatten())


def load_engine(
    models_dir: Path, *, download: bool, limits: FaceLimits | None = None, say: Any = print
) -> OpenCvFaceEngine:
    """The engine with verified models; downloads them first when `download` is set."""
    from vidette.people.models import ensure_models

    try:
        import cv2  # noqa: F401
    except ImportError as exc:
        raise EngineUnavailableError(
            "face recognition needs OpenCV: pip install 'vidette[people]' "
            "(in a source checkout: uv sync --extra people)"
        ) from exc
    detector, recognizer = ensure_models(models_dir, download=download, say=say)
    return OpenCvFaceEngine(detector, recognizer, limits)
