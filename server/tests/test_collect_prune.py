"""`vidette collect --prune-oldest`: the oldest clips nobody needs make room under the cap.

Datasets are built in a temp folder with sparse files (a "50 MB" clip takes no disk), a
real serve database (the migrations of `vidette.db.schema`) and a real identities schema.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from vidette.__main__ import main
from vidette.collect.clips import clip_id
from vidette.collect.collector import Collector, CollectSettings
from vidette.collect.disk import MB, DiskLimits, measure
from vidette.collect.prune import (
    ALERT,
    FACES,
    LABELLED,
    NEAR_ALERT,
    PRUNE_AT,
    PRUNE_TO,
    RETRY_S,
    ProtectionError,
    Pruner,
    PruneSettings,
    serve_stores,
    stored_clips,
)
from vidette.collect.target import RtspTarget
from vidette.db.schema import MIGRATIONS
from vidette.people import sidecar
from vidette.people.identities import _V1 as IDENTITIES_SCHEMA

NOW = 1_800_000_000.0  # a fixed "now"; clips are placed relative to it
DAY = 86400.0
OLD = NOW - 3 * DAY  # well past the 24 h minimum age
# serve's config may need secrets from the environment elsewhere; pruning never reads them
SECRET_ELSEWHERE = 'notifications:\n  telegram:\n    token: "${UNSET_TOKEN}"\n'


@pytest.fixture(autouse=True)
def _private_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default people and identities folders in the temp dir, never the user's."""
    monkeypatch.setenv("VIDETTE_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("VIDETTE_CONFIG", raising=False)


def add_clip(root: Path, at: float, *, mb: float = 50, early: bool = True) -> str:
    """A collected clip with everything made from it: sidecar, frames, faces, early."""
    cid = clip_id(at)
    clips = root / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    with (clips / f"{cid}.mp4").open("wb") as video:
        video.truncate(int(mb * MB))
    (clips / f"{cid}.json").write_text(json.dumps({"id": cid, "clip": f"clips/{cid}.mp4"}))
    for sub in ("frames", sidecar.FACES_DIR):
        folder = root / sub / cid
        folder.mkdir(parents=True)
        (folder / "1.jpg").write_bytes(b"j" * 1000)
    if early:
        (root / ".early").mkdir(exist_ok=True)
        (root / ".early" / f"{cid}.mp4").write_bytes(b"e" * 1000)
        (root / ".early" / f"{cid}.json").write_text("{}")
    return cid


def leftovers(root: Path, cid: str) -> list[Path]:
    paths = [
        *root.glob(f"clips/{cid}.*"),
        root / "frames" / cid,
        root / "faces" / cid,
        root / ".early" / f"{cid}.mp4",
        root / ".early" / f"{cid}.json",
    ]
    return [path for path in paths if path.exists()]


def pruner(root: Path, *, config: Path | None = None, clock: list[float] | None = None) -> Pruner:
    now = clock if clock is not None else [NOW]
    return Pruner(root, PruneSettings(serve_config=config), clock=lambda: now[0])


def limits(cap: int) -> DiskLimits:
    return DiskLimits(max_dataset_bytes=cap, min_free_bytes=0)


def labels(root: Path, *ids: str) -> None:
    entries = "".join(
        f"  - id: {cid}\n    scenario: unlabelled\n    status: draft\n    number: {n}\n"
        f"    media: [clips/{cid}.mp4]\n"
        for n, cid in enumerate(ids, start=1)
    )
    (root / "labels.yaml").write_text(f"version: 1\nname: test\nentries:\n{entries}")


def serve_db(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    for migration in MIGRATIONS:
        connection.executescript(migration)
    return connection


def event(
    db: sqlite3.Connection,
    event_id: str,
    clip: Path,
    at: float,
    *,
    state: str = "observed",
    feedback: str | None = None,
    intent: dict[str, object] | None = None,
) -> None:
    db.execute(
        "INSERT INTO events (id, camera, started_at, ended_at, state, kinds, zones, geometry,"
        " intent, feedback, source, source_ref, clip_path)"
        " VALUES (?, 'cam', ?, ?, ?, '[]', '[]', '{}', ?, ?, 'clip-folder', ?, ?)",
        (event_id, at, at + 20, state, json.dumps(intent) if intent else None, feedback,
         str(clip), str(clip)),
    )  # fmt: skip


def verdict(*, alert: bool, model_said_alert: bool, level: str | None) -> dict[str, object]:
    return {
        "verdicts": [
            {"policy": "home", "alert": alert, "model_said_alert": model_said_alert,
             "level": level, "label": "passer_by"}
        ]
    }  # fmt: skip


def serve_config(tmp_path: Path, database: Path, *, extra: str = "") -> Path:
    path = tmp_path / "serve.yaml"
    path.write_text(
        f"storage:\n  database: {database}\n  media_dir: {tmp_path / 'media'}\n"
        f"people:\n  enabled: true\n  dir: {tmp_path / 'people'}\n"
        f"  identities: {{dir: {tmp_path / 'identities'}}}\n{extra}"
    )
    return path


# --- headroom and derived files ---------------------------------------------------------


async def test_prunes_the_oldest_with_everything_made_from_them_down_to_the_target(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "porch"
    ids = [add_clip(root, OLD + i * 600) for i in range(19)]
    (root / "review").mkdir()
    (root / "review" / "sheet-0001-0008.png").write_bytes(b"png")
    cap = 1000 * MB
    disk = measure(root, limits(cap))
    assert disk.dataset_bytes > cap * PRUNE_AT and Pruner.wanted(disk)

    settings = CollectSettings(dataset=root, limits=limits(cap), prune=PruneSettings())
    collector = Collector(RtspTarget.parse("rtsp://u:p@cam/live0", "test"), settings,
                          clock=lambda: NOW)  # fmt: skip
    with caplog.at_level(logging.INFO):
        after = await collector._prune(disk)

    assert after.dataset_bytes <= cap * PRUNE_TO
    gone = [cid for cid in ids if not (root / "clips" / f"{cid}.mp4").exists()]
    assert gone == ids[: len(gone)] and gone  # the oldest, in order
    for cid in gone:
        assert leftovers(root, cid) == [], cid
    for cid in ids[len(gone) :]:
        assert len(leftovers(root, cid)) == 6, cid
    # no more than needed: keeping back the last one pruned would leave it above the target
    last = 50 * MB
    assert after.dataset_bytes + last > cap * PRUNE_TO
    assert (root / "review" / "sheet-0001-0008.png").exists()
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("pruned"))
    assert f"pruned {len(gone)} oldest clip(s)" in line and "freed" in line
    assert "oldest kept" in line and "(--max-size)" in line

    # headroom: the next clips do not prune again until the folder is past PRUNE_AT
    add_clip(root, OLD + 20 * 600)
    assert not Pruner.wanted(measure(root, limits(cap)))


def test_nothing_is_pruned_below_the_threshold(tmp_path: Path) -> None:
    root = tmp_path / "porch"
    for i in range(18):
        add_clip(root, OLD + i * 600)
    disk = measure(root, limits(1000 * MB))  # 90 %
    assert not Pruner.wanted(disk)
    assert Pruner.wanted(disk, need_bytes=200 * MB)  # unless the next clip needs more room


def test_the_ring_is_never_touched(tmp_path: Path) -> None:
    root = tmp_path / "porch"
    for i in range(4):
        add_clip(root, OLD + i * 600)
    (root / "ring").mkdir()
    segment = root / "ring" / "20270101T000000+0000.mkv"
    segment.write_bytes(b"r" * 1000)
    result = pruner(root).run(measure(root, limits(10 * MB), skip=("ring",)))
    assert result.pruned == 4 and segment.exists()


# --- what is kept -----------------------------------------------------------------------


def test_protected_clips_survive_and_unprotected_old_ones_go(tmp_path: Path) -> None:
    root = tmp_path / "porch"
    times = [OLD + i * 600 for i in range(11)]
    c = [add_clip(root, at) for at in times]
    young = add_clip(root, NOW - 3600)
    labels(root, c[0])

    database = tmp_path / "vidette.db"
    db = serve_db(database)
    clip = {cid: root / "clips" / f"{cid}.mp4" for cid in c}
    event(db, "e1", clip[c[1]], times[1], state="confirmed")
    event(db, "e2", clip[c[2]], times[2])
    db.execute("INSERT INTO telegram_messages (chat_id, event_id, kind, created_at)"
               " VALUES (1, 'e2', 'alert', ?)", (NOW,))  # fmt: skip
    event(db, "e3", root / ".early" / f"{c[3]}.mp4", times[3],
          intent=verdict(alert=False, model_said_alert=True, level="elevated"))  # fmt: skip
    event(db, "e4", clip[c[4]], times[4], feedback="down")
    event(db, "e5", clip[c[5]], times[5], state="dismissed",
          intent=verdict(alert=False, model_said_alert=False, level="none"))  # fmt: skip
    event(db, "e8", clip[c[8]], times[8], state="dismissed")
    # another camera alerted 30 s after c[9] started: c[9] is its other camera
    event(db, "e9", tmp_path / "camera2" / "clips" / "x.mp4", times[9] + 30, state="confirmed")
    db.commit()
    db.close()

    identities = tmp_path / "identities"
    identities.mkdir()
    ids_db = sqlite3.connect(identities / "identities.db")
    ids_db.executescript(IDENTITIES_SCHEMA)
    ids_db.execute("INSERT INTO identities (created_at, updated_at) VALUES (?, ?)", (NOW, NOW))
    ids_db.execute(
        "INSERT INTO sightings (identity, at, camera, source, source_id, clip, faces, face_px)"
        " VALUES (1, ?, 'cam', 'clip-folder', ?, ?, 1, 60)",
        (times[6], str(clip[c[6]]), str(root / ".early" / f"{c[6]}.mp4")),
    )
    ids_db.commit()
    ids_db.close()

    person = tmp_path / "people" / "anna"
    person.mkdir(parents=True)
    (person / "faces.json").write_text(json.dumps({"version": 1, "faces": [
        {"id": "f1", "source": "photo:a.jpg#1"},
        {"id": "f2", "source": f"confirmed:clip:{c[7]}#1"},
    ]}))  # fmt: skip
    (person / "person.json").write_text(json.dumps({"rejected": [{"where": "event:e8"}]}))

    config = serve_config(tmp_path, database, extra=SECRET_ELSEWHERE)
    disk = measure(root, limits(1 * MB))
    result = pruner(root, config=config).run(disk)

    left = {clip.id for clip in stored_clips(root)}
    assert left == {*c[:5], *c[6:10], young}, sorted(left)
    assert result.pruned == 2 and result.error is None
    assert leftovers(root, c[5]) == [] and leftovers(root, c[10]) == []
    assert result.protected == {LABELLED: 1, ALERT: 4, FACES: 3, NEAR_ALERT: 1}
    assert result.young == 1
    assert "1 labelled" in result.line() and "1 younger than 24.0 h" in result.line()


def test_nothing_prunable_keeps_the_stop_and_says_why(tmp_path: Path) -> None:
    root = tmp_path / "porch"
    ids = [add_clip(root, OLD + i * 600) for i in range(3)]
    labels(root, *ids)
    cap = 100 * MB
    clock = [NOW]
    prune = pruner(root, clock=clock)
    settings = CollectSettings(dataset=root, limits=limits(cap), prune=PruneSettings())
    collector = Collector(RtspTarget.parse("rtsp://u:p@cam/live0", "test"), settings)
    collector._pruner = prune

    disk = measure(root, limits(cap))
    result = prune.run(disk)
    assert result.pruned == 0 and len(stored_clips(root)) == 3
    blocked = disk.blocked_reason
    assert blocked is not None and "(--max-size)" in blocked
    assert collector._with_prune_note(blocked, disk).endswith(
        "; nothing left to prune (kept: 3 labelled, 0 younger than 24.0 h)"
    )
    assert not prune.due()  # a futile try waits before the next one
    clock[0] += RETRY_S
    assert prune.due()


def test_unreadable_labels_prune_nothing(tmp_path: Path) -> None:
    root = tmp_path / "porch"
    for i in range(3):
        add_clip(root, OLD + i * 600)
    (root / "labels.yaml").write_text("entries: [\n  - id: x\n")
    prune = pruner(root)
    result = prune.run(measure(root, limits(1 * MB)))
    assert result.pruned == 0 and len(stored_clips(root)) == 3
    assert prune.note is not None and "labels.yaml cannot be read" in prune.note


def test_a_missing_serve_database_prunes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "porch"
    add_clip(root, OLD)
    config = serve_config(tmp_path, tmp_path / "missing.db")
    prune = pruner(root, config=config)
    assert prune.run(measure(root, limits(1 * MB))).pruned == 0
    assert prune.note is not None and "does not exist" in prune.note


def test_face_decisions_about_events_need_the_serve_database(tmp_path: Path) -> None:
    root = tmp_path / "porch"
    add_clip(root, OLD)
    person = tmp_path / "data" / "people" / "anna"
    person.mkdir(parents=True)
    (person / "person.json").write_text(json.dumps({"rejected": [{"where": "event:e1"}]}))
    prune = pruner(root)  # no serve config: default people folder, no database
    assert prune.run(measure(root, limits(1 * MB))).pruned == 0
    assert prune.note is not None and "--serve-config" in prune.note


def test_serve_config_reads_only_storage_and_people(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = serve_config(tmp_path, tmp_path / "v.db", extra=SECRET_ELSEWHERE)
    stores = serve_stores(config)
    assert stores.database == tmp_path / "v.db"
    assert stores.people_dir == tmp_path / "people"
    assert stores.identities_dir == tmp_path / "identities"
    config.write_text("storage:\n  database: ${UNSET_DB}/v.db\n")
    with pytest.raises(ProtectionError, match="UNSET_DB"):
        serve_stores(config)
    monkeypatch.setenv("UNSET_DB", str(tmp_path))
    assert serve_stores(config).database == tmp_path / "v.db"


def test_faces_dir_matches_the_people_sidecar() -> None:
    from vidette.collect import prune

    assert prune.FACES_DIR == sidecar.FACES_DIR


def test_cli_check_previews_a_prune_without_deleting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg")
    root = tmp_path / "d"
    for i in range(3):
        add_clip(root, time.time() - 3 * DAY + i * 600)
    database = tmp_path / "vidette.db"
    serve_db(database).close()
    config = serve_config(tmp_path, database)
    monkeypatch.setenv("VIDETTE_RTSP_URL", "rtsp://u:p@127.0.0.1:9/live0")
    code = main([
        "collect", str(root), "--check", "--min-free", "1B", "--max-size", "1GB",
        "--prune-oldest", "--serve-config", str(config),
    ])  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0, out
    assert "prune    on: past 950 MB the oldest clips go until 900 MB" in out
    assert "3 clip(s), 150 MB could go now" in out
    assert len(stored_clips(root)) == 3
