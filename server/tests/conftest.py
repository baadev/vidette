"""Shared fixtures for the M1 test suite."""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from vidette.core.config import VidetteConfig
from vidette.db import Database

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None, reason="ffmpeg/ffprobe not installed"
)


@pytest.fixture
def media_dir(tmp_path: Path) -> Path:
    path = tmp_path / "media"
    path.mkdir()
    return path


@pytest.fixture
def test_config(tmp_path: Path, media_dir: Path) -> VidetteConfig:
    """One rtsp camera + tmp storage; keep in sync with schema defaults, not the example."""
    return VidetteConfig.model_validate(
        {
            "storage": {
                "media_dir": str(media_dir),
                "database": str(tmp_path / "vidette.db"),
            },
            "cameras": {
                "front-door": {
                    "adapter": "rtsp",
                    "source": {
                        "main": "rtsp://user:pw@203.0.113.10:554/stream1",
                        "sub": "rtsp://user:pw@203.0.113.10:554/stream2",
                    },
                }
            },
        }
    )


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "test.db")
    await database.connect()
    yield database
    await database.close()
