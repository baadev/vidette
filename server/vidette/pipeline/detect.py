"""Tier 1 — object detection: ONNX Runtime backend (docs/architecture/ai-pipeline.md).

The detector answers one cheap question during motion: is it a person / vehicle / animal /
package? Everything else is deliberately dropped — semantics are Tier 3's job.

Pieces, each independently testable:

- ``REGISTRY`` / ``ModelSpec`` — pinned, permissively licensed models (ADR-0006: no AGPL);
- ``ModelStore`` — atomic download + sha256 verification with a stamp-file cache;
- ``preprocess`` / ``decode_predictions`` / ``nms`` — pure numpy, no model required;
- ``OnnxDetector`` — session lifecycle + provider selection per ``DetectorConfig.hardware``;
- ``NullDetector`` — stand-in when the real detector cannot start, so the system can degrade
  to motion-only (the integrator decides when to use it — and must do so loudly).

Budget note: YOLOX-Tiny at 416×416 targets the T1 line in ai-pipeline.md (10–30 ms/frame on
the reference iGPU); inference runs in a worker thread so the recorder's event loop never
blocks on it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import onnxruntime  # type: ignore[import-untyped]
from numpy.typing import NDArray

from vidette.core.config import DetectorConfig, Hardware
from vidette.pipeline.base import BBox, Detection

logger = logging.getLogger(__name__)

__all__ = [
    "COCO_TO_VIDETTE",
    "REGISTRY",
    "DetectorError",
    "ModelSpec",
    "ModelStore",
    "NullDetector",
    "OnnxDetector",
    "decode_predictions",
    "nms",
    "preprocess",
    "resolve_model",
]


class DetectorError(Exception):
    """Tier 1 cannot proceed; the message says what to do next."""


# --- model registry ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSpec:
    key: str
    display_name: str
    url: str
    sha256: str
    input_size: int
    license: str


REGISTRY: dict[str, ModelSpec] = {
    "yolox-tiny": ModelSpec(
        key="yolox-tiny",
        display_name="YOLOX-Tiny",
        url=(
            "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/"
            "0.1.1rc0/yolox_tiny.onnx"
        ),
        sha256="427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7",
        input_size=416,
        license="Apache-2.0",
    ),
}

_AUTO_MODEL_KEY = "yolox-tiny"


def resolve_model(name: str) -> ModelSpec:
    """Map a config model name ('auto' included) to a pinned spec."""
    key = _AUTO_MODEL_KEY if name == "auto" else name
    spec = REGISTRY.get(key)
    if spec is None:
        known = ", ".join(sorted(REGISTRY))
        raise DetectorError(
            f"unknown detector model '{name}' — set understanding.detector.model to 'auto' "
            f"or one of: {known}"
        )
    return spec


# --- class mapping ----------------------------------------------------------------------------

# COCO class index (YOLOX/COCO-80 order) → Vidette label. Anything absent here is dropped:
# the M2 class list is deliberately short (person | vehicle | animal | package), and COCO has
# no 'package' class — that label arrives with a package-capable model, not by stretching COCO.
COCO_TO_VIDETTE: dict[int, str] = {
    0: "person",
    1: "vehicle",  # bicycle
    2: "vehicle",  # car
    3: "vehicle",  # motorcycle
    5: "vehicle",  # bus
    7: "vehicle",  # truck
    8: "vehicle",  # boat
    14: "animal",  # bird
    15: "animal",  # cat
    16: "animal",  # dog
    17: "animal",  # horse
    18: "animal",  # sheep
    19: "animal",  # cow
    21: "animal",  # bear
}


# --- model store ------------------------------------------------------------------------------

_DOWNLOAD_CHUNK = 1 << 16
_DOWNLOAD_TIMEOUT_S = 60.0


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_DOWNLOAD_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


class ModelStore:
    """Downloads pinned models into ``models_dir`` and verifies them by sha256.

    A ``<key>.onnx.sha256`` stamp file caches a successful verification so boot does not
    rehash ~20 MB every time; if the stamp is missing the file is rehashed (and re-stamped),
    and a hash mismatch always triggers delete + re-download.
    """

    def __init__(self, models_dir: Path, *, transport: httpx.BaseTransport | None = None) -> None:
        self._models_dir = models_dir
        self._transport = transport  # injection point for tests (httpx.MockTransport)

    def model_path(self, spec: ModelSpec) -> Path:
        return self._models_dir / f"{spec.key}.onnx"

    def _stamp_path(self, spec: ModelSpec) -> Path:
        return self._models_dir / f"{spec.key}.onnx.sha256"

    async def ensure(self, spec: ModelSpec) -> Path:
        """Return a verified local path for ``spec``, downloading it if needed."""
        return await asyncio.to_thread(self._ensure_sync, spec)

    def _ensure_sync(self, spec: ModelSpec) -> Path:
        path = self.model_path(spec)
        stamp = self._stamp_path(spec)
        if path.exists():
            if stamp.exists() and stamp.read_text(encoding="utf-8").strip() == spec.sha256:
                return path
            digest = _sha256_file(path)
            if digest == spec.sha256:
                stamp.write_text(spec.sha256, encoding="utf-8")
                return path
            logger.warning(
                "model file %s fails verification (expected sha256 %s, got %s) — re-downloading",
                path,
                spec.sha256,
                digest,
            )
            path.unlink()
            stamp.unlink(missing_ok=True)
        self._models_dir.mkdir(parents=True, exist_ok=True)
        self._download(spec, path)
        stamp.write_text(spec.sha256, encoding="utf-8")
        logger.info("downloaded %s (%s) to %s", spec.display_name, spec.license, path)
        return path

    def _download(self, spec: ModelSpec, path: Path) -> None:
        digest = hashlib.sha256()
        fd, tmp_name = tempfile.mkstemp(
            dir=self._models_dir, prefix=f".{spec.key}.", suffix=".part"
        )
        tmp = Path(tmp_name)
        try:
            with (
                os.fdopen(fd, "wb") as fh,
                httpx.Client(
                    transport=self._transport,
                    follow_redirects=True,
                    timeout=_DOWNLOAD_TIMEOUT_S,
                ) as client,
                client.stream("GET", spec.url) as response,
            ):
                response.raise_for_status()
                for chunk in response.iter_bytes(_DOWNLOAD_CHUNK):
                    digest.update(chunk)
                    fh.write(chunk)
            got = digest.hexdigest()
            if got != spec.sha256:
                raise DetectorError(
                    f"downloaded {spec.display_name} from {spec.url} but its sha256 is {got}, "
                    f"expected {spec.sha256}. The file was discarded. Retry; if this persists, "
                    "something between you and GitHub is altering the download (proxy, captive "
                    "portal) or the release asset changed — verify before updating the pin."
                )
            tmp.replace(path)
        except httpx.HTTPError as exc:
            raise DetectorError(
                f"could not download {spec.display_name} from {spec.url}: {exc} — check that "
                f"the server has outbound network access, or place a verified copy at {path} "
                "manually"
            ) from exc
        finally:
            tmp.unlink(missing_ok=True)


# --- pure numpy pre/post ----------------------------------------------------------------------

_STRIDES = (8, 16, 32)
_PAD_VALUE = 114.0
_NMS_IOU = 0.45
_CONF_THRESHOLD = 0.35


def _resize_bilinear(image: NDArray[np.uint8], out_h: int, out_w: int) -> NDArray[np.float32]:
    """Pure-numpy bilinear resize (pixel-center aligned, like cv2.INTER_LINEAR)."""
    in_h, in_w = image.shape[:2]
    source = image.astype(np.float32)
    if (in_h, in_w) == (out_h, out_w):
        return source
    ys = np.clip((np.arange(out_h, dtype=np.float32) + 0.5) * (in_h / out_h) - 0.5, 0, in_h - 1)
    xs = np.clip((np.arange(out_w, dtype=np.float32) + 0.5) * (in_w / out_w) - 0.5, 0, in_w - 1)
    y0 = np.floor(ys).astype(np.intp)
    x0 = np.floor(xs).astype(np.intp)
    y1 = np.minimum(y0 + 1, in_h - 1)
    x1 = np.minimum(x0 + 1, in_w - 1)
    wy = (ys - y0)[:, None, None]
    wx = (xs - x0)[None, :, None]
    top = source[y0][:, x0] * (1.0 - wx) + source[y0][:, x1] * wx
    bottom = source[y1][:, x0] * (1.0 - wx) + source[y1][:, x1] * wx
    result: NDArray[np.float32] = (top * (1.0 - wy) + bottom * wy).astype(np.float32)
    return result


def preprocess(frame_bgr: NDArray[np.uint8], input_size: int) -> tuple[NDArray[np.float32], float]:
    """YOLOX-style letterbox: fit into ``input_size``² with 114-gray padding, no normalization.

    YOLOX release ONNX models take raw 0–255 float32 BGR in CHW order. Returns the
    ``(1, 3, S, S)`` blob and the resize ratio (input px per frame px) for ``decode_predictions``.
    """
    if frame_bgr.ndim != 3 or frame_bgr.shape[2] != 3:
        raise DetectorError(f"expected an HxWx3 BGR frame, got array of shape {frame_bgr.shape}")
    height, width = frame_bgr.shape[:2]
    ratio = min(input_size / height, input_size / width)
    resized_h, resized_w = int(height * ratio), int(width * ratio)
    padded = np.full((input_size, input_size, 3), _PAD_VALUE, dtype=np.float32)
    padded[:resized_h, :resized_w] = _resize_bilinear(frame_bgr, resized_h, resized_w)
    blob = np.ascontiguousarray(padded.transpose(2, 0, 1)[np.newaxis], dtype=np.float32)
    return blob, ratio


def nms(
    boxes_xyxy: NDArray[np.float32], scores: NDArray[np.float32], iou_threshold: float
) -> list[int]:
    """Pure-numpy non-maximum suppression; returns kept indices, best score first."""
    x1, y1 = boxes_xyxy[:, 0], boxes_xyxy[:, 1]
    x2, y2 = boxes_xyxy[:, 2], boxes_xyxy[:, 3]
    areas = np.maximum(x2 - x1, 0.0) * np.maximum(y2 - y1, 0.0)
    order = np.argsort(scores)[::-1]
    keep: list[int] = []
    while order.size > 0:
        best = int(order[0])
        keep.append(best)
        rest = order[1:]
        if rest.size == 0:
            break
        inter_w = np.maximum(np.minimum(x2[best], x2[rest]) - np.maximum(x1[best], x1[rest]), 0.0)
        inter_h = np.maximum(np.minimum(y2[best], y2[rest]) - np.maximum(y1[best], y1[rest]), 0.0)
        inter = inter_w * inter_h
        iou = inter / (areas[best] + areas[rest] - inter + 1e-9)
        order = rest[iou <= iou_threshold]
    return keep


def _grids_and_strides(input_size: int) -> tuple[NDArray[np.float32], NDArray[np.float32]]:
    """Anchor-free YOLOX grid: per-prediction cell coordinates and stride columns."""
    grids: list[NDArray[np.float32]] = []
    strides: list[NDArray[np.float32]] = []
    for stride in _STRIDES:
        size = input_size // stride
        xv, yv = np.meshgrid(np.arange(size), np.arange(size))
        grid = np.stack((xv, yv), axis=2).reshape(-1, 2).astype(np.float32)
        grids.append(grid)
        strides.append(np.full((grid.shape[0], 1), stride, dtype=np.float32))
    return np.concatenate(grids, axis=0), np.concatenate(strides, axis=0)


def decode_predictions(
    raw: NDArray[np.float32],
    input_size: int,
    ratio: float,
    conf_threshold: float,
    frame_w: int,
    frame_h: int,
) -> list[Detection]:
    """Decode YOLOX raw output (1, N, 85) into Vidette detections.

    Standard YOLOX demo math — xy = (pred + grid) * stride, wh = exp(pred) * stride — then
    obj·cls confidence, class-aware NMS (IoU 0.45), COCO→Vidette label mapping (unmapped
    classes dropped), clipping to the frame, and normalization to 0..1.
    """
    preds = np.asarray(raw, dtype=np.float32)
    if preds.ndim == 3:
        preds = preds[0]
    grid, strides = _grids_and_strides(input_size)
    if preds.ndim != 2 or preds.shape[0] != grid.shape[0] or preds.shape[1] < 6:
        raise DetectorError(
            f"unexpected detector output shape {np.asarray(raw).shape} for input size "
            f"{input_size} — expected (1, {grid.shape[0]}, 85); the model file may not be a "
            "YOLOX export (delete it from the models directory to re-download)"
        )

    centers = (preds[:, 0:2] + grid) * strides
    sizes = np.exp(preds[:, 2:4]) * strides
    class_scores = preds[:, 4:5] * preds[:, 5:]
    class_ids = np.argmax(class_scores, axis=1)
    confidences = class_scores[np.arange(class_ids.shape[0]), class_ids]

    mapped = np.array([int(c) in COCO_TO_VIDETTE for c in class_ids], dtype=bool)
    selected = (confidences >= conf_threshold) & mapped
    if not bool(np.any(selected)):
        return []
    centers, sizes = centers[selected], sizes[selected]
    class_ids, confidences = class_ids[selected], confidences[selected]

    # Frame-pixel xyxy (undo the letterbox ratio). For class-aware NMS, shift every box
    # diagonally by class_id · (a distance larger than any coordinate span): boxes of
    # different classes can then never overlap, so one plain NMS pass is per-class.
    half = sizes / 2.0
    boxes = np.concatenate(((centers - half) / ratio, (centers + half) / ratio), axis=1)
    span = float(max(frame_w, frame_h, float(boxes.max()) - min(float(boxes.min()), 0.0))) + 1.0
    offset = class_ids.astype(np.float32)[:, None] * span
    kept = nms((boxes + offset).astype(np.float32), confidences, _NMS_IOU)

    detections: list[Detection] = []
    for index in kept:
        x1 = float(np.clip(boxes[index, 0], 0.0, frame_w))
        y1 = float(np.clip(boxes[index, 1], 0.0, frame_h))
        x2 = float(np.clip(boxes[index, 2], 0.0, frame_w))
        y2 = float(np.clip(boxes[index, 3], 0.0, frame_h))
        if x2 <= x1 or y2 <= y1:
            continue
        detections.append(
            Detection(
                label=COCO_TO_VIDETTE[int(class_ids[index])],
                confidence=float(confidences[index]),
                bbox=BBox(
                    x=x1 / frame_w,
                    y=y1 / frame_h,
                    w=(x2 - x1) / frame_w,
                    h=(y2 - y1) / frame_h,
                ),
            )
        )
    detections.sort(key=lambda d: d.confidence, reverse=True)
    return detections


# --- execution provider selection --------------------------------------------------------------

_CPU_PROVIDER = "CPUExecutionProvider"
_HARDWARE_PROVIDERS: dict[Hardware, list[str]] = {
    Hardware.cpu: [],
    Hardware.cuda: ["CUDAExecutionProvider"],
    Hardware.openvino: ["OpenVINOExecutionProvider"],
    Hardware.coreml: ["CoreMLExecutionProvider"],
}


def _preferred_providers(hardware: Hardware, platform: str = sys.platform) -> list[str]:
    """Ordered ONNX Runtime provider preference for a hardware setting; CPU is always last."""
    if hardware is Hardware.auto:
        accel = "CoreMLExecutionProvider" if platform == "darwin" else "CUDAExecutionProvider"
        return [accel, _CPU_PROVIDER]
    accelerators = _HARDWARE_PROVIDERS.get(hardware)
    if accelerators is None:  # hailo, coral — plugin targets (M3+), not ORT providers
        logger.warning(
            "understanding.detector.hardware '%s' is a plugin target (M3+) and has no ONNX "
            "Runtime provider yet — falling back to CPU",
            hardware.value,
        )
        accelerators = []
    return [*accelerators, _CPU_PROVIDER]


def _build_session(model_path: Path, preferred: list[str]) -> tuple[Any, str]:
    """Create an InferenceSession, degrading provider choice rather than crashing.

    Returns (session, active provider name). Raises DetectorError only if even the CPU
    provider cannot load the model.
    """
    available = set(onnxruntime.get_available_providers())
    usable = [p for p in preferred if p in available]
    for missing in (p for p in preferred if p not in available):
        logger.warning(
            "ONNX Runtime provider %s is not available in this build (available: %s) — "
            "falling back toward CPU",
            missing,
            ", ".join(sorted(available)),
        )
    if not usable:
        usable = [_CPU_PROVIDER]
    try:
        session = onnxruntime.InferenceSession(str(model_path), providers=usable)
    except Exception as exc:
        if usable == [_CPU_PROVIDER]:
            raise DetectorError(
                f"ONNX Runtime could not load {model_path} even on CPU: {exc} — the file may "
                "be corrupt; delete it (and its .sha256 stamp) to force a re-download"
            ) from exc
        logger.warning(
            "ONNX Runtime session with providers %s failed (%s) — retrying on CPU", usable, exc
        )
        try:
            session = onnxruntime.InferenceSession(str(model_path), providers=[_CPU_PROVIDER])
        except Exception as cpu_exc:
            raise DetectorError(
                f"ONNX Runtime could not load {model_path} even on CPU: {cpu_exc} — the file "
                "may be corrupt; delete it (and its .sha256 stamp) to force a re-download"
            ) from cpu_exc
    provider = str(session.get_providers()[0])
    return session, provider


# --- detectors ---------------------------------------------------------------------------------


class OnnxDetector:
    """Tier 1 detector: pinned ONNX model + ONNX Runtime, inference off the event loop."""

    def __init__(self, session: Any, spec: ModelSpec, provider: str) -> None:
        self._session = session
        self._input_name: str = str(session.get_inputs()[0].name)
        self.spec = spec
        self.provider = provider

    @classmethod
    async def create(
        cls,
        cfg: DetectorConfig,
        models_dir: Path,
        *,
        store: ModelStore | None = None,
    ) -> OnnxDetector:
        """Resolve, download/verify and load the configured model.

        Raises DetectorError on any failure; the integrator catches it and degrades to
        NullDetector (motion-only) — loudly, never silently.
        """
        spec = resolve_model(cfg.model)
        model_path = await (store or ModelStore(models_dir)).ensure(spec)
        preferred = _preferred_providers(cfg.hardware)
        session, provider = await asyncio.to_thread(_build_session, model_path, preferred)
        logger.info(
            "Tier 1 detector ready: %s (%s) via %s", spec.display_name, spec.license, provider
        )
        return cls(session, spec, provider)

    async def infer(self, frame_bgr: NDArray[np.uint8]) -> list[Detection]:
        """Detect person/vehicle/animal in one BGR frame; returns normalized detections."""
        blob, ratio = preprocess(frame_bgr, self.spec.input_size)
        outputs = await asyncio.to_thread(self._session.run, None, {self._input_name: blob})
        height, width = frame_bgr.shape[:2]
        return decode_predictions(
            outputs[0], self.spec.input_size, ratio, _CONF_THRESHOLD, width, height
        )


class NullDetector:
    """Detector stand-in when the real one cannot start (download/ORT init failure).

    Always returns no detections, so the cascade degrades to motion-only. Choosing to run
    with it — and telling the user — is the integrator's job; `/system` should surface
    ``provider == "disabled"``.
    """

    provider: str = "disabled"
    spec: ModelSpec | None = None

    async def infer(self, frame_bgr: object) -> list[Detection]:
        return []
