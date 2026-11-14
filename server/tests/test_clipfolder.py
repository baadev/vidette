"""Clip folders as an incident source (ADR-0010): no Frigate, same events."""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from vidette.api.app import create_app
from vidette.clipfolder import SOURCE, ClipFolderSource, clip_incident, stamp_from_name
from vidette.core.config import ClipFolderConfig, VidetteConfig, validate_config_text
from vidette.core.events import InProcessEventBus
from vidette.runtime import AppRuntime

JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9")


class MemoryStore:
    def __init__(self) -> None:
        self.meta: dict[str, str] = {}

    async def get_meta(self, key: str) -> str | None:
        return self.meta.get(key)

    async def set_meta(self, key: str, value: str) -> None:
        self.meta[key] = value


async def _no_emit(kind: str, payload: dict[str, Any]) -> None:
    return None


def collected_clip(
    root: Path, clip_id: str, *, camera: str = "front_door", started: datetime | None = None
) -> Path:
    """What `vidette collect` leaves: clip, sidecar, key frames. `started` defaults to a
    fixed moment; pass a recent one where the code under test checks the clip's age."""
    clip = root / "clips" / f"{clip_id}.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    frames = []
    for n in (1, 2, 3, 4):
        frame = root / "frames" / clip_id / f"{n}.jpg"
        frame.parent.mkdir(parents=True, exist_ok=True)
        frame.write_bytes(JPEG)
        frames.append(f"frames/{clip_id}/{n}.jpg")
    started = started or datetime(2026, 11, 14, 21, 3, 4).astimezone()
    sidecar = {
        "version": 1,
        "id": clip_id,
        "camera": camera,
        "clip": f"clips/{clip_id}.mp4",
        "frames": frames,
        "frame_times_s": [1, 3, 5, 7],
        "started_at": started.isoformat(),
        "ended_at": (started + timedelta(seconds=8)).isoformat(),
        "duration_s": 8.0,
        "stop_reason": "stream ended",
    }
    clip.with_suffix(".json").write_text(json.dumps(sidecar))
    return clip


def age(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


def test_stamps_in_file_names() -> None:
    expected = datetime(2026, 11, 14, 21, 3, 4).timestamp()
    assert stamp_from_name("20261114-210304") == expected
    assert stamp_from_name("porch_2026-11-14_21-03-04") == expected
    assert stamp_from_name("IMG_0042") is None


def test_a_collected_clip_carries_its_sidecar_facts(tmp_path: Path) -> None:
    clip = collected_clip(tmp_path, "20261114-210304")
    folder = ClipFolderConfig(path=tmp_path, camera="fallback")
    incident = clip_incident(folder, clip)
    assert incident.source == SOURCE and incident.source_id == "clips/20261114-210304.mp4"
    assert incident.camera == "front_door"
    assert incident.ended_at is not None and incident.ended_at - incident.started_at == 8
    assert incident.media[0] == str(clip) and len(incident.media) == 5


def test_a_bare_uploaded_clip_uses_its_name_and_the_folder_camera(tmp_path: Path) -> None:
    clip = tmp_path / "upload" / "porch_2026-11-14_21-03-04.mp4"
    clip.parent.mkdir()
    clip.write_bytes(b"x")
    stamp = datetime(2026, 11, 14, 21, 3, 4).timestamp()
    os.utime(clip, (stamp + 30, stamp + 30))  # uploaded when the clip ended
    incident = clip_incident(ClipFolderConfig(path=tmp_path, camera="porch"), clip)
    assert incident.camera == "porch" and incident.media == [str(clip)]
    assert incident.started_at == stamp and incident.ended_at == stamp + 30


async def test_scan_publishes_new_settled_clips_once(tmp_path: Path) -> None:
    old = collected_clip(tmp_path, "20261101-090000")
    age(old, 3600)  # there before Vidette first looked: not an incident without backfill
    bus = InProcessEventBus()
    subscription = bus.subscribe("incident.*")
    store = MemoryStore()
    folder = ClipFolderConfig(path=tmp_path, camera="porch", settle="2s")
    source = ClipFolderSource([folder], bus, store, emit=_no_emit)

    assert await source.scan(folder) == []
    await asyncio.sleep(0.05)  # ctime granularity on some file systems
    fresh = collected_clip(tmp_path, "20261114-210304")
    assert await source.scan(folder) == [], "still settling (an upload may be in progress)"
    (tmp_path / ".partial").mkdir()
    (tmp_path / ".partial" / "x.mp4").write_bytes(b"x")  # hidden: never an incident
    source._clock = lambda: time.time() + 10  # type: ignore[method-assign]
    published = await source.scan(folder)
    assert [incident.source_id for incident in published] == ["clips/20261114-210304.mp4"]
    topics = [(await asyncio.wait_for(subscription.get(), 1))[0] for _ in range(2)]
    assert topics == ["incident.opened", "incident.closed"]
    assert await source.scan(folder) == [], "the watermark remembers what was published"

    restarted = ClipFolderSource([folder], bus, store, emit=_no_emit)
    restarted._clock = source._clock  # type: ignore[method-assign]
    assert await restarted.scan(folder) == []
    assert fresh.is_file()  # Vidette never moves or deletes anything in the folder


async def test_backfill_takes_what_is_already_there(tmp_path: Path) -> None:
    age(collected_clip(tmp_path, "20261101-090000"), 3600)
    folder = ClipFolderConfig(path=tmp_path, backfill=True, settle="0s")
    source = ClipFolderSource([folder], InProcessEventBus(), MemoryStore(), emit=_no_emit)
    assert len(await source.scan(folder)) == 1


def test_config_accepts_clip_folders_instead_of_frigate(tmp_path: Path) -> None:
    report = validate_config_text(
        f"clip_folders:\n  - path: {tmp_path}\n    camera: front_door\n"
        "policies:\n  - name: p\n    description: x\n    cameras: [front_door]\n"
    )
    assert report.valid, report.errors
    assert not any("no incident source" in warning for warning in report.warnings)
    bad = validate_config_text(f"clip_folders:\n  - path: {tmp_path}\n    poll_every: soon\n")
    assert not bad.valid and "poll_every" in bad.errors[0]


def test_wiring_a_collected_clip_becomes_an_event_with_snapshot_and_clip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIDETTE_WEB_DIST", str(tmp_path / "no-web-dist"))
    dataset = tmp_path / "front-door"
    dataset.mkdir()
    config = VidetteConfig.model_validate(
        {
            "server": {"auth": {"mode": "none"}},
            "storage": {
                "media_dir": str(tmp_path / "media"),
                "database": str(tmp_path / "vidette.db"),
            },
            "clip_folders": [{"path": str(dataset), "poll_every": "1s", "settle": "0s"}],
        }
    )
    runtime = AppRuntime(config)
    app = create_app(runtime, workers=True)
    with TestClient(app) as client:
        test_sleep = 0.05
        client.portal.call(asyncio.sleep, 0.2)  # first scan sets the watermark
        collected_clip(dataset, "20261114-210304")
        events: list[dict[str, Any]] = []
        for _ in range(80):
            events = client.get("/api/v1/events").json()
            if events and events[0]["snapshot"]:
                break
            client.portal.call(asyncio.sleep, test_sleep)
        assert len(events) == 1, events
        event = events[0]
        assert event["source"] == SOURCE and event["camera"] == "front_door"
        assert event["state"] == "observed"
        snapshot = client.get(event["snapshot"])
        assert snapshot.status_code == 200 and snapshot.content == JPEG
        clip = client.get(event["clip"])
        assert clip.status_code == 200 and clip.headers["content-type"] == "video/mp4"
        folders = client.get("/api/v1/system").json()["clip_folders"]
        assert folders[0]["clips_total"] == 1 and folders[0]["exists"] is True
