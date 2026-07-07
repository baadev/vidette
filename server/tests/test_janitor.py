"""Janitor tests: retention passes, disk watermarks, write probe, housekeeping.

No ffmpeg needed — segments are tiny real files, disk usage is monkeypatched, and the
Database/ExportManager collaborators are in-test fakes conforming to their contracts.
"""

from __future__ import annotations

import collections
import shutil
import time
from pathlib import Path
from typing import Any, cast

import pytest

from vidette.core.config import VidetteConfig
from vidette.db import Database, SegmentRow
from vidette.recording.exporter import ExportManager
from vidette.recording.janitor import Janitor

DiskUsage = collections.namedtuple("DiskUsage", "total used free")

HOUR = 3600.0
DAY = 24 * HOUR


class FakeDb:
    """In-memory segments + captured system events; conforms to the Database contract."""

    def __init__(self) -> None:
        self.rows: list[SegmentRow] = []
        self.system_events: list[tuple[str, dict[str, Any]]] = []
        self.purge_calls: list[float] = []

    async def all_segments(self) -> list[SegmentRow]:
        return list(self.rows)

    async def delete_segments_by_path(self, paths: list[str]) -> int:
        doomed = set(paths)
        before = len(self.rows)
        self.rows = [row for row in self.rows if row.path not in doomed]
        return before - len(self.rows)

    async def add_system_event(self, kind: str, payload: dict[str, Any]) -> int:
        self.system_events.append((kind, payload))
        return len(self.system_events)

    async def purge_expired_sessions(self, now: float) -> int:
        self.purge_calls.append(now)
        return 0

    async def media_bytes_total(self) -> int:
        return sum(row.size_bytes for row in self.rows)

    def paths(self) -> set[str]:
        return {row.path for row in self.rows}

    def events_of(self, kind: str) -> list[dict[str, Any]]:
        return [payload for event_kind, payload in self.system_events if event_kind == kind]


class FakeExporter:
    def __init__(self) -> None:
        self.cleanup_calls: list[float] = []

    async def cleanup_old(self, older_than_s: float = 24 * 3600) -> int:
        self.cleanup_calls.append(older_than_s)
        return 0


def make_config(media_dir: Path, tmp_path: Path) -> VidetteConfig:
    """Global retention defaults (continuous 3d); 'yard' overrides continuous to 1h."""
    return VidetteConfig.model_validate(
        {
            "storage": {
                "media_dir": str(media_dir),
                "database": str(tmp_path / "vidette.db"),
            },
            "cameras": {
                "front-door": {
                    "adapter": "rtsp",
                    "source": {"main": "rtsp://user:pw@203.0.113.10:554/stream1"},
                },
                "yard": {
                    "adapter": "rtsp",
                    "source": {"main": "rtsp://user:pw@203.0.113.11:554/stream1"},
                    "record": {"retention": {"continuous": "1h"}},
                },
            },
        }
    )


def add_segment(
    db: FakeDb,
    media_dir: Path,
    camera: str,
    *,
    age_s: float,
    klass: str = "continuous",
    size: int = 40,
    create_file: bool = True,
) -> Path:
    row_id = len(db.rows) + 1
    end = time.time() - age_s
    start = end - 10.0
    path = media_dir / camera / f"seg-{row_id}.mp4"
    if create_file:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"v" * size)
    db.rows.append(
        SegmentRow(
            id=row_id,
            camera=camera,
            start_ts=start,
            end_ts=end,
            path=str(path),
            size_bytes=size,
            klass=klass,
            codec="h264",
        )
    )
    return path


def make_janitor(
    config: VidetteConfig, db: FakeDb, exporter: FakeExporter | None = None
) -> tuple[Janitor, FakeExporter]:
    fake_exporter = exporter or FakeExporter()
    janitor = Janitor(config, cast(Database, db), cast(ExportManager, fake_exporter))
    return janitor, fake_exporter


def patch_disk(monkeypatch: pytest.MonkeyPatch, *, total: int, free: int) -> None:
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _path: DiskUsage(total=total, used=total - free, free=free)
    )


PLENTY = {"total": 1000, "free": 500}  # 50 % free: no watermark, no pressure


async def test_per_camera_retention_override_honored(
    media_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(media_dir, tmp_path)
    db = FakeDb()
    # Both 2 h old: expired under yard's 1 h override, safe under the global 3 d default.
    front_path = add_segment(db, media_dir, "front-door", age_s=2 * HOUR)
    yard_path = add_segment(db, media_dir, "yard", age_s=2 * HOUR)
    patch_disk(monkeypatch, **PLENTY)

    janitor, _ = make_janitor(config, db)
    status = await janitor.run_once()

    assert not yard_path.exists()
    assert front_path.exists()
    assert db.paths() == {str(front_path)}
    assert status.expired_deleted_total == 1
    assert status.pressure_deleted_total == 0
    assert status.last_run_at is not None


async def test_missing_file_counts_as_deleted_without_event(
    media_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(media_dir, tmp_path)
    db = FakeDb()
    add_segment(db, media_dir, "front-door", age_s=5 * DAY, create_file=False)
    patch_disk(monkeypatch, **PLENTY)

    janitor, _ = make_janitor(config, db)
    status = await janitor.run_once()

    assert status.expired_deleted_total == 1
    assert db.rows == []  # row removed even though the file was already gone
    assert db.events_of("storage.delete_failed") == []


async def test_undeletable_file_raises_delete_failed_and_keeps_row(
    media_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(media_dir, tmp_path)
    db = FakeDb()
    stubborn = add_segment(db, media_dir, "front-door", age_s=5 * DAY)
    patch_disk(monkeypatch, **PLENTY)

    real_unlink = Path.unlink

    def failing_unlink(self: Path, missing_ok: bool = False) -> None:
        if self.suffix == ".mp4":
            raise OSError(30, "Read-only file system")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", failing_unlink)

    janitor, _ = make_janitor(config, db)
    status = await janitor.run_once()

    assert status.expired_deleted_total == 0
    assert db.paths() == {str(stubborn)}  # row kept, so deletion is retried next pass
    events = db.events_of("storage.delete_failed")
    assert len(events) == 1
    assert events[0]["path"] == str(stubborn)


async def test_pressure_deletes_oldest_continuous_first_never_event_or_favorite(
    media_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(media_dir, tmp_path)
    db = FakeDb()
    oldest = add_segment(db, media_dir, "front-door", age_s=10 * HOUR, size=40)
    newer = add_segment(db, media_dir, "front-door", age_s=1 * HOUR, size=40)
    motion = add_segment(db, media_dir, "front-door", age_s=12 * HOUR, klass="motion", size=40)
    event = add_segment(db, media_dir, "front-door", age_s=20 * HOUR, klass="event", size=40)
    favorite = add_segment(
        db, media_dir, "front-door", age_s=30 * HOUR, klass="favorite", size=40
    )
    # 9 % free < 10 % pressure floor; target 12 % → free 30 bytes → one 40-byte segment.
    patch_disk(monkeypatch, total=1000, free=90)

    janitor, _ = make_janitor(config, db)
    status = await janitor.run_once()

    assert not oldest.exists()  # oldest continuous went first
    assert newer.exists()
    assert motion.exists()  # continuous alone satisfied the target
    assert event.exists() and favorite.exists()  # never pressure-deleted
    assert status.pressure_deleted_total == 1
    assert status.expired_deleted_total == 0
    assert db.events_of("storage.pressure") == []  # target met → no loud event


async def test_unmet_pressure_emits_storage_pressure_with_numbers(
    media_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(media_dir, tmp_path)
    db = FakeDb()
    event = add_segment(db, media_dir, "front-door", age_s=1 * HOUR, klass="event", size=40)
    favorite = add_segment(db, media_dir, "front-door", age_s=1 * HOUR, klass="favorite", size=40)
    # 5 % free, target 12 % → need 70 bytes, but nothing is pressure-deletable.
    patch_disk(monkeypatch, total=1000, free=50)

    janitor, _ = make_janitor(config, db)
    status = await janitor.run_once()

    assert event.exists() and favorite.exists()
    assert status.pressure_deleted_total == 0
    pressure_events = db.events_of("storage.pressure")
    assert len(pressure_events) == 1
    assert pressure_events[0]["unmet_bytes"] == 70
    assert pressure_events[0]["bytes_to_free"] == 70
    assert pressure_events[0]["free_bytes"] == 50
    assert pressure_events[0]["total_bytes"] == 1000

    # Still true next tick → no second event (deduplicated by crossing).
    await janitor.run_once()
    assert len(db.events_of("storage.pressure")) == 1


async def test_storage_low_emitted_once_per_crossing(
    media_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(media_dir, tmp_path)
    db = FakeDb()
    janitor, _ = make_janitor(config, db)

    # 14 % free: below the 15 % warn line, above the 10 % pressure floor.
    patch_disk(monkeypatch, total=1000, free=140)
    await janitor.run_once()
    await janitor.run_once()
    assert len(db.events_of("storage.low")) == 1
    assert db.events_of("storage.low")[0]["free_bytes"] == 140

    # Recover above the line, then cross again → exactly one more event.
    patch_disk(monkeypatch, total=1000, free=300)
    await janitor.run_once()
    patch_disk(monkeypatch, total=1000, free=140)
    await janitor.run_once()
    assert len(db.events_of("storage.low")) == 2


async def test_write_probe_failure_emits_storage_write_failed(
    media_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(media_dir, tmp_path)
    db = FakeDb()
    patch_disk(monkeypatch, **PLENTY)

    def failing_write_bytes(self: Path, data: bytes) -> int:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_bytes", failing_write_bytes)

    janitor, _ = make_janitor(config, db)
    status = await janitor.run_once()  # first tick probes

    assert status.last_probe_ok is False
    events = db.events_of("storage.write_failed")
    assert len(events) == 1
    assert "No space left" in str(events[0]["error"])


async def test_write_probe_success_and_cadence(
    media_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(media_dir, tmp_path)
    db = FakeDb()
    patch_disk(monkeypatch, **PLENTY)

    janitor, _ = make_janitor(config, db)
    status = await janitor.run_once()
    assert status.last_probe_ok is True
    assert not (media_dir / ".vidette-probe").exists()  # probe cleans up after itself
    assert db.events_of("storage.write_failed") == []

    # Ticks 2–5 do not probe; the 6th (tick index 5 → every 5th) does.
    monkeypatch.setattr(
        Path, "write_bytes", lambda self, data: (_ for _ in ()).throw(OSError("boom"))
    )
    for _ in range(4):
        status = await janitor.run_once()
        assert status.last_probe_ok is True  # unchanged — no probe ran
    status = await janitor.run_once()
    assert status.last_probe_ok is False
    assert len(db.events_of("storage.write_failed")) == 1


async def test_housekeeping_purges_sessions_and_cleans_exports(
    media_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(media_dir, tmp_path)
    db = FakeDb()
    patch_disk(monkeypatch, **PLENTY)

    janitor, exporter = make_janitor(config, db)
    before = time.time()
    status = await janitor.run_once()

    assert len(db.purge_calls) == 1
    assert db.purge_calls[0] >= before
    assert exporter.cleanup_calls == [24 * 3600]
    assert status.media_bytes == 0
    assert status.disk_total_bytes == 1000
    assert status.disk_free_bytes == 500


async def test_status_before_first_run_is_empty(media_dir: Path, tmp_path: Path) -> None:
    config = make_config(media_dir, tmp_path)
    janitor, _ = make_janitor(config, FakeDb())
    status = janitor.status()
    assert status.last_run_at is None
    assert status.last_probe_ok is None
    assert status.expired_deleted_total == 0
    assert status.pressure_deleted_total == 0
