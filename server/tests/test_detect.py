"""Tier 1 detector backend: pure numpy pre/post, model store, and gated real-model test."""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path

import httpx
import numpy as np
import pytest

from vidette.core.config import DetectorConfig
from vidette.pipeline.detect import (
    COCO_TO_VIDETTE,
    REGISTRY,
    DetectorError,
    ModelSpec,
    ModelStore,
    NullDetector,
    OnnxDetector,
    decode_predictions,
    nms,
    preprocess,
    resolve_model,
)

INPUT_SIZE = 416


# --- registry ---------------------------------------------------------------------------------


def test_auto_resolves_to_yolox_tiny() -> None:
    spec = resolve_model("auto")
    assert spec is REGISTRY["yolox-tiny"]
    assert spec.input_size == INPUT_SIZE
    assert spec.license == "Apache-2.0"  # ADR-0006: no AGPL in the default install
    assert len(spec.sha256) == 64


def test_unknown_model_is_actionable() -> None:
    with pytest.raises(DetectorError, match="yolox-tiny"):
        resolve_model("yolo-nonexistent")


# --- preprocess -------------------------------------------------------------------------------


def test_preprocess_letterbox_landscape_frame() -> None:
    color = (200, 150, 50)  # BGR, values > 1 prove there is no 0..1 normalization
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    frame[:] = color

    blob, ratio = preprocess(frame, INPUT_SIZE)

    assert blob.shape == (1, 3, INPUT_SIZE, INPUT_SIZE)
    assert blob.dtype == np.float32
    assert ratio == pytest.approx(INPUT_SIZE / 1280)  # width is the limiting side
    resized_h = int(720 * ratio)  # 234
    for channel in range(3):
        content = blob[0, channel, :resized_h, :]
        assert np.all(content == float(color[channel]))
    # Everything below the content is 114-gray padding.
    assert np.all(blob[0, :, resized_h:, :] == 114.0)


def test_preprocess_portrait_frame_pads_right() -> None:
    frame = np.full((400, 200, 3), 30, dtype=np.uint8)
    blob, ratio = preprocess(frame, INPUT_SIZE)
    assert ratio == pytest.approx(INPUT_SIZE / 400)
    resized_w = int(200 * ratio)  # 208
    assert np.all(blob[0, :, :, :resized_w] == 30.0)
    assert np.all(blob[0, :, :, resized_w:] == 114.0)


def test_preprocess_square_frame_no_padding() -> None:
    frame = np.full((INPUT_SIZE, INPUT_SIZE, 3), 7, dtype=np.uint8)
    blob, ratio = preprocess(frame, INPUT_SIZE)
    assert ratio == 1.0
    assert np.all(blob == 7.0)
    assert not np.any(blob == 114.0)


def test_preprocess_rejects_non_bgr_input() -> None:
    with pytest.raises(DetectorError, match="HxWx3"):
        preprocess(np.zeros((100, 100), dtype=np.uint8), INPUT_SIZE)


# --- decode -----------------------------------------------------------------------------------

_STRIDE_OFFSETS = {8: 0, 16: 52 * 52, 32: 52 * 52 + 26 * 26}


def _blank_raw() -> np.ndarray:
    return np.zeros((1, 3549, 85), dtype=np.float32)


def _inject(
    raw: np.ndarray,
    *,
    stride: int,
    cx: float,
    cy: float,
    w: float,
    h: float,
    class_id: int,
    score: float,
) -> None:
    """Write one box (input-space pixels) into the YOLOX raw tensor at its grid cell."""
    gx, gy = int(cx // stride), int(cy // stride)
    index = _STRIDE_OFFSETS[stride] + gy * (INPUT_SIZE // stride) + gx
    raw[0, index, 0] = cx / stride - gx
    raw[0, index, 1] = cy / stride - gy
    raw[0, index, 2] = math.log(w / stride)
    raw[0, index, 3] = math.log(h / stride)
    raw[0, index, 4] = 1.0  # objectness
    raw[0, index, 5 + class_id] = score


def test_decode_synthetic_boxes_end_to_end() -> None:
    # Frame is 832×832 with input 416 → ratio 0.5, so frame coords are input coords × 2.
    frame_w = frame_h = 832
    ratio = 0.5
    raw = _blank_raw()
    # Two clean persons, one car, one below-threshold person, one overlapping person pair.
    _inject(raw, stride=8, cx=100, cy=100, w=40, h=60, class_id=0, score=0.92)
    _inject(raw, stride=16, cx=300, cy=200, w=64, h=96, class_id=0, score=0.80)
    _inject(raw, stride=32, cx=208, cy=336, w=120, h=64, class_id=2, score=0.85)  # car
    _inject(raw, stride=8, cx=50, cy=350, w=40, h=40, class_id=0, score=0.20)  # reject
    _inject(raw, stride=8, cx=300, cy=300, w=80, h=80, class_id=0, score=0.88)  # NMS winner
    _inject(raw, stride=8, cx=304, cy=300, w=80, h=80, class_id=0, score=0.70)  # NMS loser

    detections = decode_predictions(raw, INPUT_SIZE, ratio, 0.35, frame_w, frame_h)

    assert [d.label for d in detections] == ["person", "person", "vehicle", "person"]
    assert [d.confidence for d in detections] == pytest.approx([0.92, 0.88, 0.85, 0.80], abs=1e-5)

    def assert_bbox(index: int, x: float, y: float, w: float, h: float) -> None:
        bbox = detections[index].bbox
        assert (bbox.x, bbox.y, bbox.w, bbox.h) == pytest.approx((x, y, w, h), abs=1e-4)

    # Expected: input-space box → frame space (÷ ratio) → normalized (÷ 832).
    assert_bbox(0, 160 / 832, 140 / 832, 80 / 832, 120 / 832)  # person (100,100,40,60)
    assert_bbox(1, 520 / 832, 520 / 832, 160 / 832, 160 / 832)  # NMS winner (300,300,80,80)
    assert_bbox(2, 296 / 832, 608 / 832, 240 / 832, 128 / 832)  # car (208,336,120,64)
    assert_bbox(3, 536 / 832, 304 / 832, 128 / 832, 192 / 832)  # person (300,200,64,96)


def test_decode_drops_unmapped_coco_classes() -> None:
    raw = _blank_raw()
    _inject(raw, stride=8, cx=100, cy=100, w=40, h=40, class_id=9, score=0.95)  # traffic light
    _inject(raw, stride=16, cx=200, cy=200, w=60, h=60, class_id=63, score=0.95)  # laptop
    assert decode_predictions(raw, INPUT_SIZE, 0.5, 0.35, 832, 832) == []


def test_decode_clips_boxes_to_frame() -> None:
    raw = _blank_raw()
    _inject(raw, stride=8, cx=5, cy=5, w=30, h=30, class_id=0, score=0.9)  # spills over 0,0
    detections = decode_predictions(raw, INPUT_SIZE, 0.5, 0.35, 832, 832)
    assert len(detections) == 1
    bbox = detections[0].bbox
    # Frame-space box is (-20,-20)–(40,40): clipped to (0,0)–(40,40), then normalized.
    assert (bbox.x, bbox.y) == pytest.approx((0.0, 0.0), abs=1e-6)
    assert (bbox.w, bbox.h) == pytest.approx((40 / 832, 40 / 832), abs=1e-4)
    assert 0.0 <= bbox.x <= 1.0 and 0.0 <= bbox.y <= 1.0
    assert bbox.x + bbox.w <= 1.0 and bbox.y + bbox.h <= 1.0


def test_decode_rejects_wrong_shape() -> None:
    with pytest.raises(DetectorError, match="delete it from the models directory"):
        decode_predictions(np.zeros((1, 100, 85), dtype=np.float32), INPUT_SIZE, 1.0, 0.35, 10, 10)


def test_coco_mapping() -> None:
    assert COCO_TO_VIDETTE[0] == "person"
    for coco_id in (1, 2, 3, 5, 7, 8):  # bicycle car motorcycle bus truck boat
        assert COCO_TO_VIDETTE[coco_id] == "vehicle"
    for coco_id in (14, 15, 16, 17, 18, 19, 21):  # bird cat dog horse sheep cow bear
        assert COCO_TO_VIDETTE[coco_id] == "animal"
    assert 9 not in COCO_TO_VIDETTE  # traffic light
    assert 63 not in COCO_TO_VIDETTE  # laptop
    assert set(COCO_TO_VIDETTE.values()) == {"person", "vehicle", "animal"}


# --- NMS --------------------------------------------------------------------------------------


def test_nms_suppresses_overlaps_keeps_distant() -> None:
    boxes = np.array(
        [[0, 0, 10, 10], [1, 1, 11, 11], [20, 20, 30, 30]],
        dtype=np.float32,
    )
    scores = np.array([0.9, 0.8, 0.7], dtype=np.float32)
    assert nms(boxes, scores, 0.45) == [0, 2]
    # A permissive threshold keeps all three, in score order.
    assert nms(boxes, scores, 0.95) == [0, 1, 2]


def test_nms_orders_by_score_not_index() -> None:
    boxes = np.array([[0, 0, 10, 10], [100, 100, 110, 110]], dtype=np.float32)
    scores = np.array([0.3, 0.9], dtype=np.float32)
    assert nms(boxes, scores, 0.45) == [1, 0]


def test_nms_empty_input() -> None:
    assert nms(np.zeros((0, 4), dtype=np.float32), np.zeros((0,), dtype=np.float32), 0.45) == []


# --- ModelStore -------------------------------------------------------------------------------

_FAKE_BYTES = b"not a real onnx model, but faithfully hashed\n" * 64


def _fake_spec(payload: bytes = _FAKE_BYTES, sha256: str | None = None) -> ModelSpec:
    return ModelSpec(
        key="fake-model",
        display_name="Fake Model",
        url="https://example.invalid/fake-model.onnx",
        sha256=sha256 or hashlib.sha256(payload).hexdigest(),
        input_size=416,
        license="Apache-2.0",
    )


def _counting_store(
    tmp_path: Path, payload: bytes = _FAKE_BYTES
) -> tuple[ModelStore, dict[str, int]]:
    counter = {"requests": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counter["requests"] += 1
        return httpx.Response(200, content=payload)

    store = ModelStore(tmp_path / "models", transport=httpx.MockTransport(handler))
    return store, counter


async def test_model_store_downloads_and_verifies(tmp_path: Path) -> None:
    store, counter = _counting_store(tmp_path)
    spec = _fake_spec()

    path = await store.ensure(spec)

    assert path == tmp_path / "models" / "fake-model.onnx"
    assert path.read_bytes() == _FAKE_BYTES
    assert counter["requests"] == 1
    stamp = path.with_name("fake-model.onnx.sha256")
    assert stamp.read_text().strip() == spec.sha256
    assert not list(path.parent.glob("*.part"))  # tmp file cleaned up


async def test_model_store_cached_second_call_skips_download(tmp_path: Path) -> None:
    store, counter = _counting_store(tmp_path)
    spec = _fake_spec()
    path = await store.ensure(spec)

    assert await store.ensure(spec) == path
    assert counter["requests"] == 1  # stamp hit: no re-download, no rehash

    # Missing stamp → rehash (still no download) and restamp.
    stamp = path.with_name("fake-model.onnx.sha256")
    stamp.unlink()
    assert await store.ensure(spec) == path
    assert counter["requests"] == 1
    assert stamp.read_text().strip() == spec.sha256


async def test_model_store_checksum_mismatch_deletes_and_raises(tmp_path: Path) -> None:
    store, counter = _counting_store(tmp_path)  # serves _FAKE_BYTES...
    spec = _fake_spec(sha256="0" * 64)  # ...but the pin expects something else

    with pytest.raises(DetectorError, match="sha256"):
        await store.ensure(spec)

    models_dir = tmp_path / "models"
    assert not (models_dir / "fake-model.onnx").exists()
    assert not (models_dir / "fake-model.onnx.sha256").exists()
    assert not list(models_dir.glob("*.part"))
    assert counter["requests"] == 1


async def test_model_store_redownloads_corrupted_local_file(tmp_path: Path) -> None:
    store, counter = _counting_store(tmp_path)
    spec = _fake_spec()
    path = await store.ensure(spec)

    # Corrupt the file and remove the stamp: ensure() must detect and re-download.
    path.write_bytes(b"bitrot")
    path.with_name("fake-model.onnx.sha256").unlink()
    assert (await store.ensure(spec)).read_bytes() == _FAKE_BYTES
    assert counter["requests"] == 2


async def test_model_store_http_error_is_actionable(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    store = ModelStore(tmp_path / "models", transport=httpx.MockTransport(handler))
    with pytest.raises(DetectorError, match="network"):
        await store.ensure(_fake_spec())
    assert not list((tmp_path / "models").glob("*"))


# --- NullDetector -----------------------------------------------------------------------------


async def test_null_detector() -> None:
    detector = NullDetector()
    assert detector.provider == "disabled"
    assert detector.spec is None
    assert await detector.infer(np.zeros((10, 10, 3), dtype=np.uint8)) == []


# --- real model (opt-in: downloads ~20 MB) ----------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("VIDETTE_TEST_MODEL") != "1",
    reason="set VIDETTE_TEST_MODEL=1 to run the real-model integration test",
)
async def test_real_model_download_and_inference(tmp_path: Path) -> None:
    models_dir = Path(os.environ.get("VIDETTE_TEST_MODELS_DIR") or tmp_path)
    detector = await OnnxDetector.create(DetectorConfig(), models_dir)

    assert detector.spec is REGISTRY["yolox-tiny"]
    assert detector.provider  # e.g. CoreMLExecutionProvider / CPUExecutionProvider

    rng = np.random.default_rng(seed=7)
    frame = rng.integers(0, 256, size=(480, 640, 3), dtype=np.uint8)
    detections = await detector.infer(frame)
    assert isinstance(detections, list)  # content unasserted: noise frames owe us nothing
