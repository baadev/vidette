"""The other cameras at an event's moment (ADR-0017): which clips count, the same person
only by face, approximate cameras, the mosaic's layout, and the bot's «📹 Другие камеры».
Clips are synthetic (ffmpeg's test pattern) where a real video is needed."""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_clipfolder import collected_clip

from vidette.core.config import ClipFolderConfig, VidetteConfig
from vidette.db import Database
from vidette.linked import describe, find_linked, is_portrait, tiles_for
from vidette.stitch import Piece, Tile, arrange, plan, stitch

T0 = datetime(2026, 11, 14, 12, 0, 0).astimezone()


def config(tmp_path: Path, **cross: Any) -> VidetteConfig:
    return VidetteConfig.model_validate(
        {
            "storage": {"media_dir": str(tmp_path / "media"), "database": str(tmp_path / "v.db")},
            "clip_folders": [
                {"path": str(tmp_path / "a"), "camera": "door"},
                {"path": str(tmp_path / "b"), "camera": "street"},
                {"path": str(tmp_path / "c"), "camera": "yard"},
            ],
            "cross_camera": cross,
        }
    )


def clip_at(root: Path, camera: str, seconds: float, length: float = 8.0) -> Path:
    started = T0 + timedelta(seconds=seconds)
    clip = collected_clip(root, started.strftime("%Y%m%d-%H%M%S"), camera=camera, started=started)
    sidecar = json.loads(clip.with_suffix(".json").read_text())
    sidecar["ended_at"] = (started + timedelta(seconds=length)).isoformat()
    sidecar["duration_s"] = length
    clip.with_suffix(".json").write_text(json.dumps(sidecar))
    return clip


async def an_event(
    db: Database, clip: Path, camera: str, seconds: float, *, identity: int | None = None
) -> str:
    event_id = f"ev-{camera}-{int(seconds)}"
    seen = []
    if identity is not None:
        seen.append({"n": 1, "status": "unknown", "score": 0.2, "identity": {"id": identity}})
    intent = {
        "verdicts": [{"label": "passer_by", "persons": 1}],
        "people": {"seen": seen},
        "timeline": {"person_seen": T0.timestamp() + seconds + 2},
    }
    await db.insert_event(
        event_id, camera, T0.timestamp() + seconds, "dismissed", [], [], {},
        ended_at=T0.timestamp() + seconds + 8, source="clip-folder",
        source_ref=clip.as_posix(), clip_path=str(clip), intent=intent,
    )  # fmt: skip
    return event_id


async def test_the_clips_around_the_moment_on_every_camera(tmp_path: Path) -> None:
    cfg = config(tmp_path, lag_up_to={"door": "2m"})
    db = Database(cfg.storage.database)
    await db.connect()
    own = clip_at(tmp_path / "b", "street", 100)
    before = clip_at(tmp_path / "b", "street", 80)  # 15 s window reaches back to 87
    same = clip_at(tmp_path / "c", "yard", 120)
    far = clip_at(tmp_path / "c", "yard", 400)
    late = clip_at(tmp_path / "a", "door", 200)  # stamped late, may show the moment
    event_id = await an_event(db, own, "street", 100, identity=7)
    await an_event(db, same, "yard", 120, identity=7)
    await an_event(db, before, "street", 80, identity=8)
    row = await db.get_event(event_id)
    assert row is not None

    linked = await find_linked(row, cfg, db)
    by_path = {clip.path: clip for clip in linked.clips}
    assert linked.clips[0].own and linked.clips[0].path == own
    assert linked.window.moment == T0.timestamp() + 102, "when the person was first seen"
    assert set(by_path) == {own, before, same, late} and far not in by_path
    assert by_path[same].same_person, "the same identity in both: the same person"
    assert not by_path[before].same_person, "a different face is not the same person"
    assert by_path[late].approximate and not by_path[same].approximate
    lines = describe(linked, camera_name=str.upper, configured=["door", "street", "yard"])
    assert any("YARD" in line and "тот же человек по лицу" in line for line in lines)
    assert any(line.startswith("DOOR: около") for line in lines)

    quiet = config(tmp_path)  # no lag: the late door clip does not count
    alone = await find_linked(row, quiet, db, cameras=["street", "door"])
    assert {c.camera for c in alone.clips} == {"street"}
    silent = describe(alone, camera_name=str, configured=["door", "street"])
    assert silent[-1].startswith("door: в это время ничего не записано")
    await db.close()


def test_the_mosaic_never_shrinks_a_portrait_camera_to_a_sliver() -> None:
    cells, width, height = arrange([False, False, True])
    assert (width, height) == (640 + 404, 720)
    assert [(c.x, c.y, c.w, c.h) for c in cells] == [
        (0, 0, 640, 360), (0, 360, 640, 360), (640, 0, 404, 720),
    ]  # fmt: skip
    cells, width, height = arrange([False, False, False])
    assert (width, height) == (640, 1080), "three landscape cameras: one tall column"
    cells, width, height = arrange([False, True])
    assert height == 640 and cells[1].x == cells[0].w


def test_a_plan_pads_where_a_camera_recorded_nothing(tmp_path: Path) -> None:
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    tiles = [
        Tile("street", [Piece(a, 10.0, 18.0), Piece(a, 30.0, 40.0)]),
        Tile("yard", [Piece(b, 0.0, 100.0)]),
        Tile("door", [Piece(b, 500.0, 510.0)]),  # nothing in the window: left out
    ]
    inputs, graph, cameras, cells, _w, _h = plan(tiles, 5.0, 35.0)
    assert cameras == ["street", "yard"] and len(cells) == 2
    assert inputs.count("-i") == 3
    assert graph.count("color=c=") == 2, "a gap before the first clip and between the two"
    assert "concat=n=4" in graph and "xstack=inputs=2" in graph
    at = inputs.index(str(b))
    assert inputs[at - 5 : at] == ["-ss", "5.000", "-t", "30.000", "-i"], "yard: the window"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
async def test_a_real_mosaic_of_two_cameras(tmp_path: Path) -> None:
    async def video(path: Path, size: str, seconds: int) -> None:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi",
            "-i", f"testsrc2=size={size}:rate=15", "-t", str(seconds), "-pix_fmt", "yuv420p",
            str(path),
        )  # fmt: skip
        assert await proc.wait() == 0

    wide, tall = tmp_path / "wide.mp4", tmp_path / "tall.mp4"
    await video(wide, "320x180", 6)
    await video(tall, "180x320", 4)
    tiles = [Tile("door", [Piece(wide, 100.0, 106.0)]),
             Tile("street", [Piece(tall, 103.0, 107.0)], portrait=True)]  # fmt: skip
    made = await stitch(tiles, 99.0, 108.0, tmp_path / "out" / "m.mp4")
    assert made.files == [tmp_path / "out" / "m.mp4"] and made.size_mb < 1
    assert made.cameras == ["door", "street"] and made.place(1) == ("right", "full")
    assert (made.width, made.height) == (1138 + 360, 640)


def test_portrait_comes_from_the_sidecar(tmp_path: Path) -> None:
    clip = clip_at(tmp_path, "street", 0)
    assert not is_portrait(clip)
    sidecar = json.loads(clip.with_suffix(".json").read_text())
    sidecar.update(width=1920, height=1080, rotation=270)
    clip.with_suffix(".json").write_text(json.dumps(sidecar))
    assert is_portrait(clip)


async def test_tiles_put_the_events_camera_first(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    db = Database(cfg.storage.database)
    await db.connect()
    own = clip_at(tmp_path / "c", "yard", 100)
    clip_at(tmp_path / "a", "door", 95)
    row = await db.get_event(await an_event(db, own, "yard", 100))
    assert row is not None
    tiles = tiles_for(await find_linked(row, cfg, db))
    assert [t.camera for t in tiles] == ["yard", "door"]
    await db.close()


async def test_the_bot_sends_the_other_cameras_or_says_they_slept(tmp_path: Path) -> None:
    from test_telegram import alert, bot, callback, event

    b = await bot(tmp_path)
    street = tmp_path / "street"
    b.config.clip_folders.append(
        ClipFolderConfig(path=street, camera="street")
    )  # a second camera: the button appears
    event_id = await event(b, "20260115-101500")
    [row] = await alert(b, event_id)
    buttons = b.fake.last("sendMessage").params["reply_markup"]["inline_keyboard"]
    assert buttons[-1] == [{"text": "📹 Другие камеры", "callback_data": f"a:{row.id}:x"}]

    await b.service.handle_update(callback(b, row, "x"))
    assert b.fake.last("answerCallbackQuery").params["text"] == "Собираю видео с других камер…"
    await asyncio.gather(*b.service._work)
    said = b.fake.last("sendMessage").params
    assert "другие камеры ничего не записали" in said["text"]
    assert "street: в это время ничего не записано" in said["text"]
    assert said["reply_parameters"]["message_id"] == row.message_id
    await b.db.close()
