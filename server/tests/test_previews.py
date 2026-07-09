"""PreviewWorker tests: real-ffmpeg hour generation + hermetic guard paths + the endpoint.

Real segments are built with ffmpeg (testsrc2) at their proper hour paths; the Database is
an in-test fake conforming to the `segments_between` contract, and the clock is injected so
the segment hour counts as fully elapsed. Endpoint tests mount the recordings router on a
bare FastAPI app with a SimpleNamespace runtime, auth overridden at `current_principal`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from conftest import FFMPEG, FFPROBE, requires_ffmpeg
from fastapi import FastAPI
from fastapi.testclient import TestClient

import vidette.auth.deps as auth_deps
from vidette.auth.service import ANONYMOUS_ADMIN
from vidette.core.config import VidetteConfig
from vidette.db import Database, SegmentRow
from vidette.recording.previews import PreviewWorker, preview_path
from vidette.recording.segments import segment_hour_dir

HOUR = 3600
HOUR0 = 1_751_900_400.0  # exact UTC hour boundary (multiple of 3600)
NOW = HOUR0 + HOUR + 120.0  # two minutes past the end of the HOUR0 hour — fully elapsed


class FakeDb:
    """Just enough Database for the worker: overlap query over an in-memory list."""

    def __init__(self, rows: list[SegmentRow]) -> None:
        self.rows = rows

    async def segments_between(
        self, camera: str, start_ts: float, end_ts: float
    ) -> list[SegmentRow]:
        return sorted(
            (
                row
                for row in self.rows
                if row.camera == camera and row.end_ts > start_ts and row.start_ts < end_ts
            ),
            key=lambda row: row.start_ts,
        )


def make_worker(
    config: VidetteConfig,
    rows: list[SegmentRow],
    media_dir: Path,
    *,
    ffmpeg: str | None = None,
    now: float = NOW,
) -> PreviewWorker:
    # sys.executable stands in for ffmpeg in hermetic tests that never reach the spawn.
    return PreviewWorker(
        config,
        cast(Database, FakeDb(rows)),
        media_dir=media_dir,
        ffmpeg_path=ffmpeg or sys.executable,
        clock=lambda: now,
    )


def make_row(row_id: int, camera: str, start_ts: float, path: Path) -> SegmentRow:
    return SegmentRow(
        id=row_id,
        camera=camera,
        start_ts=start_ts,
        end_ts=start_ts + 2.0,
        path=str(path),
        size_bytes=path.stat().st_size if path.exists() else 0,
        klass="continuous",
        codec="h264",
    )


def make_clip(path: Path, seconds: float = 2.0) -> None:
    assert FFMPEG is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            FFMPEG,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=320x240:rate=10:duration={seconds}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(path),
        ],
        check=True,
        timeout=60,
    )


def make_hour_segments(media_dir: Path, camera: str, hour_start: float) -> list[SegmentRow]:
    """Two real 2 s segments at their proper `<camera>/YYYY/MM/DD/HH/<epoch>.mp4` paths."""
    hour_dir = segment_hour_dir(media_dir / camera, hour_start)
    rows: list[SegmentRow] = []
    for i, offset in enumerate((0, 2)):
        clip = hour_dir / f"{int(hour_start) + offset}.mp4"
        make_clip(clip)
        rows.append(make_row(i + 1, camera, hour_start + offset, clip))
    return rows


def _ffprobe(path: Path, *args: str) -> str:
    assert FFPROBE is not None
    out = subprocess.run(
        [FFPROBE, "-v", "error", *args, "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return out.stdout.strip()


def probe_duration(path: Path) -> float:
    return float(_ffprobe(path, "-show_entries", "format=duration"))


def probe_height(path: Path) -> int:
    return int(_ffprobe(path, "-select_streams", "v:0", "-show_entries", "stream=height"))


# --- worker: real ffmpeg -------------------------------------------------------------------


@requires_ffmpeg
async def test_run_once_generates_preview_and_is_idempotent(
    test_config: VidetteConfig, media_dir: Path
) -> None:
    rows = make_hour_segments(media_dir, "front-door", HOUR0)
    assert FFMPEG is not None
    worker = make_worker(test_config, rows, media_dir, ffmpeg=FFMPEG)

    assert await worker.run_once() == 1
    out = preview_path(media_dir, "front-door", HOUR0)
    assert out == media_dir / "front-door" / "previews" / f"{int(HOUR0)}.mp4"
    assert out.is_file()

    status = worker.status()
    assert status.last_run_at == NOW
    assert status.generated_total == 1
    assert status.last_error is None

    # 1 fps over 4 s of footage: a short but non-empty strip, downscaled to 180 px tall.
    assert probe_duration(out) > 0
    assert probe_duration(out) <= 6.0
    assert probe_height(out) == 180

    # Concat list and tmp output never linger; only the finished preview remains.
    assert sorted(p.name for p in out.parent.iterdir()) == [out.name]

    # Second pass: the file exists, so nothing regenerates.
    assert await worker.run_once() == 0
    assert worker.status().generated_total == 1


@requires_ffmpeg
async def test_segment_outside_media_dir_is_skipped_but_hour_still_generates(
    test_config: VidetteConfig, media_dir: Path, tmp_path: Path
) -> None:
    rows = make_hour_segments(media_dir, "front-door", HOUR0)[:1]  # one 2 s clip inside
    outside = tmp_path / "outside.mp4"
    make_clip(outside)
    rows.append(make_row(2, "front-door", HOUR0 + 2.0, outside))
    assert FFMPEG is not None
    worker = make_worker(test_config, rows, media_dir, ffmpeg=FFMPEG)

    assert await worker.run_once() == 1
    out = preview_path(media_dir, "front-door", HOUR0)
    assert out.is_file()
    # Only the inside clip (2 s → ~2 frames at 1 fps) made it into the strip.
    assert 0 < probe_duration(out) <= 3.0


# --- worker: hermetic guard paths ------------------------------------------------------------


async def test_hour_with_all_segments_outside_media_dir_yields_nothing(
    test_config: VidetteConfig, media_dir: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x")  # never opened — the path guard rejects it first
    rows = [make_row(1, "front-door", HOUR0, outside)]
    worker = make_worker(test_config, rows, media_dir)

    assert await worker.run_once() == 0
    assert not preview_path(media_dir, "front-door", HOUR0).exists()
    error = worker.status().last_error
    assert error is not None and "media directory" in error


async def test_generate_hour_without_segments_returns_none(
    test_config: VidetteConfig, media_dir: Path
) -> None:
    worker = make_worker(test_config, [], media_dir)
    assert await worker.generate_hour("front-door", HOUR0) is None
    assert not (media_dir / "front-door" / "previews").exists()


async def test_incomplete_current_hour_is_not_generated(
    test_config: VidetteConfig, media_dir: Path
) -> None:
    rows = [make_row(1, "front-door", HOUR0, media_dir / "front-door" / "a.mp4")]
    worker = make_worker(test_config, rows, media_dir, now=HOUR0 + 1800.0)  # mid-hour
    assert await worker.run_once() == 0
    assert not (media_dir / "front-door" / "previews").exists()


async def test_missing_ffmpeg_generates_nothing_and_explains(
    test_config: VidetteConfig, media_dir: Path
) -> None:
    rows = [make_row(1, "front-door", HOUR0, media_dir / "front-door" / "a.mp4")]
    worker = make_worker(test_config, rows, media_dir, ffmpeg="vidette-ffmpeg-that-does-not-exist")
    assert await worker.run_once() == 0
    status = worker.status()
    assert status.last_run_at == NOW
    assert status.last_error is not None and "ffmpeg" in status.last_error
    assert not (media_dir / "front-door" / "previews").exists()


async def test_start_stop_task_lifecycle(test_config: VidetteConfig, media_dir: Path) -> None:
    worker = make_worker(test_config, [], media_dir, ffmpeg="vidette-ffmpeg-that-does-not-exist")
    await worker.start()
    await worker.stop()
    await worker.stop()  # stop is idempotent


# --- endpoint --------------------------------------------------------------------------------


def make_client(config: VidetteConfig) -> TestClient:
    from vidette.api.routers import recordings

    app = FastAPI()
    app.include_router(recordings.router)
    app.state.runtime = SimpleNamespace(config=config)
    app.dependency_overrides[auth_deps.current_principal] = lambda: ANONYMOUS_ADMIN
    return TestClient(app)


def test_preview_endpoint_serves_mp4(test_config: VidetteConfig, media_dir: Path) -> None:
    out = preview_path(media_dir, "front-door", HOUR0)
    out.parent.mkdir(parents=True)
    out.write_bytes(b"preview-bytes")
    response = make_client(test_config).get(
        "/api/v1/recordings/preview",
        params={"camera": "front-door", "hour_start_ts": HOUR0},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"
    assert response.content == b"preview-bytes"


def test_preview_endpoint_missing_file_is_problem_404(test_config: VidetteConfig) -> None:
    response = make_client(test_config).get(
        "/api/v1/recordings/preview",
        params={"camera": "front-door", "hour_start_ts": HOUR0},
    )
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["type"] == "about:blank"
    assert detail["title"] == "Preview not available"
    assert "preview not generated yet" in detail["detail"]
    assert "~5 minutes" in detail["detail"]


def test_preview_endpoint_unknown_camera_404(test_config: VidetteConfig) -> None:
    response = make_client(test_config).get(
        "/api/v1/recordings/preview",
        params={"camera": "ghost", "hour_start_ts": HOUR0},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["title"] == "Camera not found"
