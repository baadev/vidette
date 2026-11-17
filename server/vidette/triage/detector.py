"""People, vehicles and animals in a frame: a small object detector, on this machine.

`ObjectDetector` is the boundary; `YoloxDetector` is the only implementation that needs
OpenCV (`pip install 'vidette[people]'`, the same extra as the face check) and tests use
a fake. The model is YOLOX-S (Apache-2.0, trained on COCO) from the OpenCV model zoo at
the commit the face models come from, checked byte for byte before every load
(vidette.people.models). It runs on the CPU through OpenCV's DNN module: about 0.1 s for
a frame on an Apple-silicon machine with all cores, a few tenths with the two threads
`vidette serve` allows it.

Only three kinds of COCO's eighty classes matter to the pre-filter: `person`,
`vehicle` (car, truck, bus, motorcycle, bicycle) and `animal` (bird, cat, dog and the
farm animals). Everything else is dropped where it is found.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from vidette.people.models import ModelFile, ensure_model

Kind = Literal["person", "vehicle", "animal"]

YOLOX_S = ModelFile(
    name="object_detection_yolox_2022nov.onnx",
    url=(
        "https://github.com/opencv/opencv_zoo/raw/48b10fa505aeb51fbd127fc60a5c660040e823b7"
        "/models/object_detection_yolox/object_detection_yolox_2022nov.onnx"
    ),
    sha256="c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063",
    size=35_858_002,
    license="Apache-2.0",
    what="YOLOX-S object detector (COCO)",
    command="vidette triage models",
    kind="detector model",
)
DETECTOR_NAME = "yolox-s-2022nov"

# COCO class index → kind (the rest of the 80 classes is dropped)
_KINDS: dict[int, tuple[Kind, str]] = {
    0: ("person", "person"),
    1: ("vehicle", "bicycle"),
    2: ("vehicle", "car"),
    3: ("vehicle", "motorcycle"),
    5: ("vehicle", "bus"),
    7: ("vehicle", "truck"),
    14: ("animal", "bird"),
    15: ("animal", "cat"),
    16: ("animal", "dog"),
    17: ("animal", "horse"),
    18: ("animal", "sheep"),
    19: ("animal", "cow"),
    21: ("animal", "bear"),
}


class DetectorUnavailableError(Exception):
    """OpenCV is not installed or the model cannot be loaded; the message says how."""


@dataclass(frozen=True)
class Detection:
    kind: Kind
    label: str  # the COCO class: person, car, cat, …
    score: float
    box: tuple[float, float, float, float]  # x, y, width, height as fractions of the frame

    @property
    def area(self) -> float:
        return self.box[2] * self.box[3]

    def as_list(self) -> list[Any]:
        return [self.label, round(self.score, 3), [round(v, 4) for v in self.box]]

    @classmethod
    def from_list(cls, raw: list[Any]) -> Detection | None:
        """The inverse of `as_list` (an observation read back from the database)."""
        try:
            label, score, box = str(raw[0]), float(raw[1]), [float(v) for v in raw[2]]
        except (IndexError, TypeError, ValueError):
            return None
        kind = next((k for k, name in _KINDS.values() if name == label), None)
        if kind is None or len(box) != 4:
            return None
        return cls(kind, label, score, (box[0], box[1], box[2], box[3]))


class ObjectDetector(Protocol):
    @property
    def name(self) -> str: ...

    def detect(self, image: bytes) -> list[Detection]: ...


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    w = min(ax2, bx2) - max(a[0], b[0])
    h = min(ay2, by2) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


iou = _iou


class YoloxDetector:
    """YOLOX-S through OpenCV DNN. Thread-safe (one inference at a time)."""

    SIZE = 640
    STRIDES = (8, 16, 32)

    def __init__(self, model: Path, *, min_score: float = 0.2, threads: int | None = 2) -> None:
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise DetectorUnavailableError(
                "the object detector needs OpenCV: pip install 'vidette[people]'"
            ) from exc
        self._cv2: Any = cv2
        self._np: Any = np
        if threads is not None:
            cv2.setNumThreads(max(1, threads))
        try:
            self._net = cv2.dnn.readNet(str(model))
        except cv2.error as exc:
            raise DetectorUnavailableError(f"OpenCV cannot load {model.name}: {exc}") from exc
        self._min_score = min_score
        self._lock = threading.Lock()
        grids, strides = [], []
        for stride in self.STRIDES:
            n = self.SIZE // stride
            xv, yv = np.meshgrid(np.arange(n), np.arange(n))
            grid = np.stack((xv, yv), 2).reshape(-1, 2)
            grids.append(grid)
            strides.append(np.full((grid.shape[0], 1), stride))
        self._grids = np.concatenate(grids, 0)
        self._strides = np.concatenate(strides, 0)

    @property
    def name(self) -> str:
        return DETECTOR_NAME

    def detect(self, image: bytes) -> list[Detection]:
        cv2, np = self._cv2, self._np
        bgr = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_COLOR)
        if bgr is None:
            return []
        height, width = bgr.shape[:2]
        ratio = min(self.SIZE / height, self.SIZE / width)
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        padded = np.full((self.SIZE, self.SIZE, 3), 114.0, dtype=np.float32)
        h, w = int(height * ratio), int(width * ratio)
        padded[:h, :w] = cv2.resize(rgb, (w, h)).astype(np.float32)
        blob = np.transpose(padded, (2, 0, 1))[np.newaxis]
        with self._lock:
            self._net.setInput(blob)
            out = self._net.forward(self._net.getUnconnectedOutLayersNames())[0][0].copy()
        out[:, :2] = (out[:, :2] + self._grids) * self._strides
        out[:, 2:4] = np.exp(out[:, 2:4]) * self._strides
        boxes = out[:, :4].copy()
        boxes[:, 0] -= boxes[:, 2] / 2
        boxes[:, 1] -= boxes[:, 3] / 2
        scores = out[:, 4:5] * out[:, 5:]
        best = scores.max(axis=1)
        classes = scores.argmax(axis=1)
        wanted = np.isin(classes, list(_KINDS)) & (best >= self._min_score)
        if not wanted.any():
            return []
        boxes, best, classes = boxes[wanted], best[wanted], classes[wanted]
        keep = cv2.dnn.NMSBoxesBatched(
            boxes.tolist(), best.tolist(), classes.tolist(), self._min_score, 0.5
        )
        found: list[Detection] = []
        for index in np.array(keep).reshape(-1):
            x, y, bw, bh = (float(v) / ratio for v in boxes[index])
            kind, label = _KINDS[int(classes[index])]
            box = (
                max(0.0, x / width),
                max(0.0, y / height),
                min(1.0, bw / width),
                min(1.0, bh / height),
            )
            found.append(Detection(kind, label, float(best[index]), box))
        return sorted(found, key=lambda d: -d.score)


def default_models_dir() -> Path:
    from vidette.people.models import default_models_dir as people_default

    return people_default()


def load_detector(
    models_dir: Path | None = None,
    *,
    download: bool = False,
    threads: int | None = 2,
    say: Any = print,
) -> YoloxDetector:
    """The detector with its verified model; downloads only when asked (the command
    line), never inside `vidette serve`."""
    from vidette.people.models import ModelError

    folder = models_dir or default_models_dir()
    try:
        path = ensure_model(YOLOX_S, folder, download=download, say=say)
    except ModelError as exc:
        raise DetectorUnavailableError(str(exc)) from exc
    return YoloxDetector(path, threads=threads)
