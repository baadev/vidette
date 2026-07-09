"""Reconciliation tests: orphaned segment files on disk vs the segments table.

Uses a real Database and (where marked) real ffmpeg/ffprobe — the whole point is that a
genuine finalized MP4 is adopted and a truncated tail is recognized as garbage.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from vidette.db import Database
from vidette.recording.reconcile import reconcile_camera_segments
from vidette.recording.segments import camera_media_dir, segment_hour_dir

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
requires_ffmpeg: pytest.MarkDecorator = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None, reason="ffmpeg/ffprobe not installed"
)


def _write_segment_file(camera_dir: Path, epoch: int, content: bytes, *, age_s: float) -> Path:
    hour_dir = segment_hour_dir(camera_dir, epoch)
    hour_dir.mkdir(parents=True, exist_ok=True)
    path = hour_dir / f"{epoch}.mp4"
    path.write_bytes(content)
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))
    return path


def _real_mp4_bytes(tmp_path: Path) -> bytes:
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        [
            FFMPEG or "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x120:rate=10",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(clip),
        ],
        check=True,
        timeout=60,
    )
    return clip.read_bytes()


@requires_ffmpeg
async def test_reconcile_indexes_readable_orphans_and_removes_garbage(
    tmp_path: Path, db: Database
) -> None:
    camera_dir = camera_media_dir(tmp_path / "media", "front-door")
    epoch = int(time.time()) - 3600
    good = _write_segment_file(camera_dir, epoch, _real_mp4_bytes(tmp_path), age_s=600)
    # A truncated tail: ffmpeg was killed before writing the moov atom — unplayable.
    bad = _write_segment_file(camera_dir, epoch + 10, b"\x00" * 4096, age_s=600)

    result = await reconcile_camera_segments(db, "front-door", camera_dir)

    assert (result.indexed, result.removed, result.skipped) == (1, 1, 0)
    assert good.exists()
    assert not bad.exists()
    row = await db.latest_segment("front-door")
    assert row is not None
    assert row.path == str(good)
    assert row.start_ts == float(epoch)
    assert 0.5 <= row.end_ts - row.start_ts <= 2.0  # the clip is ~1 s long
    assert row.klass == "continuous"


@requires_ffmpeg
async def test_reconcile_leaves_indexed_and_fresh_files_alone(
    tmp_path: Path, db: Database
) -> None:
    camera_dir = camera_media_dir(tmp_path / "media", "front-door")
    epoch = int(time.time()) - 3600
    known = _write_segment_file(camera_dir, epoch, _real_mp4_bytes(tmp_path), age_s=600)
    await db.add_segment(
        camera="front-door",
        start_ts=float(epoch),
        end_ts=float(epoch + 1),
        path=str(known),
        size_bytes=known.stat().st_size,
    )
    # Garbage, but too fresh — a writer may still own it; must survive untouched.
    fresh = _write_segment_file(camera_dir, epoch + 20, b"\x00" * 4096, age_s=5)

    result = await reconcile_camera_segments(db, "front-door", camera_dir)

    assert (result.indexed, result.removed, result.skipped) == (0, 0, 1)
    assert known.exists() and fresh.exists()


async def test_reconcile_ignores_previews_and_missing_dir(tmp_path: Path, db: Database) -> None:
    camera_dir = camera_media_dir(tmp_path / "media", "front-door")
    previews = camera_dir / "previews"
    previews.mkdir(parents=True)
    stray = previews / "1783609200.mp4"
    stray.write_bytes(b"\x00" * 128)
    os.utime(stray, (time.time() - 600, time.time() - 600))

    result = await reconcile_camera_segments(db, "front-door", camera_dir)
    assert (result.indexed, result.removed) == (0, 0)
    assert stray.exists()  # previews are not segment storage

    empty = await reconcile_camera_segments(db, "ghost", tmp_path / "media" / "ghost")
    assert (empty.indexed, empty.removed, empty.skipped) == (0, 0, 0)
