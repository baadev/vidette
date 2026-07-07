"""Export manager tests: real-ffmpeg round trip + hermetic validation/error paths."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

import pytest
from conftest import FFMPEG, FFPROBE, requires_ffmpeg

from vidette.core.config import VidetteConfig
from vidette.db import Database, SegmentRow
from vidette.recording.exporter import MAX_RANGE_S, ExportError, ExportJob, ExportManager

T0 = 1_751_900_000.0  # arbitrary fixed epoch for segment timestamps


class FakeDb:
    """Just enough Database for the exporter: overlap query over an in-memory list."""

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


def make_manager(
    config: VidetteConfig, rows: list[SegmentRow], media_dir: Path, *, ffmpeg: str | None = None
) -> ExportManager:
    # sys.executable stands in for ffmpeg in hermetic tests that never reach the spawn.
    return ExportManager(
        config,
        cast(Database, FakeDb(rows)),
        media_dir=media_dir,
        ffmpeg_path=ffmpeg or sys.executable,
    )


def make_row(
    row_id: int, camera: str, start_ts: float, path: Path, seconds: float = 2.0
) -> SegmentRow:
    return SegmentRow(
        id=row_id,
        camera=camera,
        start_ts=start_ts,
        end_ts=start_ts + seconds,
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


def ffprobe_duration(path: Path) -> float:
    assert FFPROBE is not None
    out = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return float(out.stdout.strip())


async def wait_for_finish(
    manager: ExportManager, job_id: str, timeout_s: float = 20.0
) -> ExportJob:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        assert job is not None
        if job.state in ("done", "error"):
            return job
        await asyncio.sleep(0.1)
    pytest.fail(f"export job did not finish within {timeout_s} s")


# --- integration (real ffmpeg) -----------------------------------------------------------------


@requires_ffmpeg
async def test_export_two_segments_roundtrip(test_config: VidetteConfig, media_dir: Path) -> None:
    seg_dir = media_dir / "front-door" / "2026" / "07" / "07" / "12"
    clip_a = seg_dir / f"{int(T0)}.mp4"
    clip_b = seg_dir / f"{int(T0) + 2}.mp4"
    make_clip(clip_a)
    make_clip(clip_b)
    rows = [
        make_row(1, "front-door", T0, clip_a),
        make_row(2, "front-door", T0 + 2.0, clip_b),
    ]
    assert FFMPEG is not None
    manager = make_manager(test_config, rows, media_dir, ffmpeg=FFMPEG)
    await manager.start()
    try:
        job = await manager.create("front-door", T0, T0 + 4.0)
        assert job.state == "queued"
        assert manager.get(job.id) is job

        finished = await wait_for_finish(manager, job.id)
        assert finished.state == "done", finished.error
        assert finished.error is None
        assert finished.path == media_dir / "exports" / f"{job.id}.mp4"
        assert finished.path.is_file()
        assert finished.size_bytes == finished.path.stat().st_size
        assert finished.size_bytes > 0

        # The concat list file is always cleaned up.
        assert list((media_dir / "exports").glob("*.txt")) == []

        # Two 2 s clips concatenated — allow container/keyframe slack.
        assert 3.5 <= ffprobe_duration(finished.path) <= 5.0
    finally:
        await manager.stop()


@requires_ffmpeg
async def test_ffmpeg_failure_yields_error_with_stderr(
    test_config: VidetteConfig, media_dir: Path
) -> None:
    bogus = media_dir / "front-door" / "not-a-video.mp4"
    bogus.parent.mkdir(parents=True, exist_ok=True)
    bogus.write_bytes(b"this is not an mp4")
    rows = [make_row(1, "front-door", T0, bogus)]
    assert FFMPEG is not None
    manager = make_manager(test_config, rows, media_dir, ffmpeg=FFMPEG)
    await manager.start()
    try:
        job = await manager.create("front-door", T0, T0 + 2.0)
        finished = await wait_for_finish(manager, job.id)
        assert finished.state == "error"
        assert finished.error is not None and "ffmpeg" in finished.error
        assert not (media_dir / "exports" / f"{job.id}.mp4").exists()
    finally:
        await manager.stop()


# --- hermetic error paths (no ffmpeg spawned) ---------------------------------------------------


async def test_create_rejects_unknown_camera(
    test_config: VidetteConfig, media_dir: Path
) -> None:
    manager = make_manager(test_config, [], media_dir)
    with pytest.raises(ExportError, match="front-door"):
        await manager.create("garage", T0, T0 + 10.0)


async def test_create_rejects_empty_and_reversed_range(
    test_config: VidetteConfig, media_dir: Path
) -> None:
    manager = make_manager(test_config, [], media_dir)
    with pytest.raises(ExportError, match="empty"):
        await manager.create("front-door", T0, T0)
    with pytest.raises(ExportError, match="empty"):
        await manager.create("front-door", T0 + 10.0, T0)


async def test_create_rejects_too_long_range(
    test_config: VidetteConfig, media_dir: Path
) -> None:
    manager = make_manager(test_config, [], media_dir)
    with pytest.raises(ExportError, match="shorter"):
        await manager.create("front-door", T0, T0 + MAX_RANGE_S + 1.0)


async def test_create_rejects_range_without_footage(
    test_config: VidetteConfig, media_dir: Path
) -> None:
    manager = make_manager(test_config, [], media_dir)
    with pytest.raises(ExportError, match="no recorded footage"):
        await manager.create("front-door", T0, T0 + 10.0)


async def test_segment_path_outside_media_dir_errors_the_job(
    test_config: VidetteConfig, media_dir: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x")
    rows = [make_row(1, "front-door", T0, outside)]
    manager = make_manager(test_config, rows, media_dir)
    await manager.start()
    try:
        job = await manager.create("front-door", T0, T0 + 2.0)
        finished = await wait_for_finish(manager, job.id, timeout_s=10.0)
        assert finished.state == "error"
        assert finished.error is not None and "media directory" in finished.error
        assert not (media_dir / "exports" / f"{job.id}.mp4").exists()
    finally:
        await manager.stop()


async def test_get_unknown_job_returns_none(
    test_config: VidetteConfig, media_dir: Path
) -> None:
    manager = make_manager(test_config, [], media_dir)
    assert manager.get("deadbeefdeadbeef") is None


async def test_cleanup_old_sweeps_orphans_and_finished_jobs(
    test_config: VidetteConfig, media_dir: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x")
    rows = [make_row(1, "front-door", T0, outside)]
    manager = make_manager(test_config, rows, media_dir)
    await manager.start()
    try:
        job = await manager.create("front-door", T0, T0 + 2.0)
        finished = await wait_for_finish(manager, job.id, timeout_s=10.0)
        assert finished.state == "error"
    finally:
        await manager.stop()

    exports = media_dir / "exports"
    orphan = exports / "0123456789abcdef.mp4"
    orphan.write_bytes(b"old export from before a restart")
    old = time.time() - 48 * 3600
    os.utime(orphan, (old, old))
    fresh = exports / "fedcba9876543210.mp4"
    fresh.write_bytes(b"fresh orphan stays")
    future = time.time() + 3600
    os.utime(fresh, (future, future))  # mtime clearly after the cutoff — no timing races

    removed = await manager.cleanup_old(older_than_s=0.0)
    # The finished (error) job record + the stale orphan file; the fresh file survives.
    assert removed == 2
    assert manager.get(job.id) is None
    assert not orphan.exists()
    assert fresh.exists()
