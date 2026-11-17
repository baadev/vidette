"""`vidette eval` on what a service already said: stored live and deep verdicts read from
its database (no model call), a service's policy as ground truth, several datasets scored
as one, and false alarms and misses per day."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from test_eval import write_dataset

from vidette.__main__ import main
from vidette.db import Database
from vidette.eval.dataset import DatasetError, load_dataset, union, with_policy
from vidette.eval.stored import StoredJudge
from vidette.verdicts.base import PolicyText

SCENARIOS = {
    "passer_by": "walks past",
    "person_at_house": "stops at the house",
    "touching_equipment": "touches the panel",
}
POLICY = {
    "name": "home",
    "description": "A house.",
    "levels": {
        "critical": {"wake": "always", "scenarios": {"touching_equipment": "panel"}},
        "elevated": {"wake": "unless_known", "scenarios": {"person_at_house": "stops"}},
        "none": {"wake": "never", "scenarios": {"passer_by": "walks past"}},
    },
}


def clip_set(root: Path, name: str, entries: list[tuple[str, str, str]]) -> Path:
    """A dataset with one clip per entry: (id, scenario, ISO time)."""
    (root / "clips").mkdir(parents=True, exist_ok=True)
    rows = []
    for entry_id, scenario, at in entries:
        (root / "clips" / f"{entry_id}.mp4").write_bytes(b"\0")
        rows.append(
            {"id": entry_id, "scenario": scenario, "status": "reviewed", "at": at,
             "camera": name, "media": [f"clips/{entry_id}.mp4"]}
        )  # fmt: skip
    return write_dataset(
        root,
        {
            "version": 1, "name": name, "license": "test", "observed_hours": 48,
            "scenarios": SCENARIOS,
            "policies": {"old": {"description": "x", "alert_on": ["touching_equipment"]}},
            "entries": rows,
        },
    )  # fmt: skip


def verdict(label: str, alert: bool, **extra: Any) -> dict[str, Any]:
    return {"policy": "home", "label": label, "alert": alert, "abstained": False,
            "confidence": 0.9, "model": "m", "latency_s": 40.0, **extra}  # fmt: skip


Seeded = list[tuple[str, dict[str, Any] | None, dict[str, Any] | None]]


async def seed(database: Path, root: Path, rows: Seeded) -> None:
    db = Database(database)
    await db.connect()
    for n, (entry_id, live, deep) in enumerate(rows):
        clip = str((root / "clips" / f"{entry_id}.mp4").resolve())
        intent = None if live is None else {"verdicts": [live]}
        state = "confirmed" if live and live["alert"] else "dismissed"
        await db.insert_event(
            f"{root.name}-{n}", root.name, 1_800_000_000.0 + n, state, [], [], {},
            intent=intent, clip_path=clip, source="clip-folder", source_ref=clip,
        )  # fmt: skip
        if deep is not None:
            await db.set_event_deep(f"{root.name}-{n}", {"verdicts": [deep]})
    await db.close()


@pytest.fixture
def place(tmp_path: Path) -> tuple[Path, Path, Path]:
    door = clip_set(tmp_path / "data" / "door", "door", [
        ("d1", "person_at_house", "2026-01-14T10:00:00+00:00"),
        ("d2", "passer_by", "2026-01-14T11:00:00+00:00"),
        ("d3", "passer_by", "2026-01-15T11:00:00+00:00"),
        ("d4", "touching_equipment", "2026-01-15T12:00:00+00:00"),
    ])  # fmt: skip
    yard = clip_set(tmp_path / "data" / "yard", "yard", [
        ("y1", "passer_by", "2026-01-15T09:00:00+00:00"),
    ])  # fmt: skip
    database = tmp_path / "v.db"
    asyncio.run(seed(database, door, [
        ("d1", verdict("person_at_house", True), verdict("person_at_house", True)),
        ("d2", verdict("person_at_house", True), verdict("passer_by", False)),
        ("d3", {**verdict("passer_by", False), "abstained": True}, verdict("passer_by", False)),
    ]))  # fmt: skip
    asyncio.run(seed(database, yard, [("y1", verdict("passer_by", False), None)]))
    return door, yard, database


async def test_stored_judges_say_what_the_service_said(place: tuple[Path, Path, Path]) -> None:
    door, _yard, database = place
    dataset = load_dataset(door)
    policy = PolicyText("home", "A house.")
    live = StoredJudge.from_database("stored-live", database)
    deep = StoredJudge.from_database("stored-deep", database)
    best = StoredJudge.from_database("stored-best", database)
    by_id = {e.id: dataset.case(e) for e in dataset.labels.entries}
    said = await live.judge(by_id["d1"], policy)
    assert said.alert and said.label == "person_at_house" and said.vlm_calls == 0
    assert (await live.judge(by_id["d3"], policy)).rationale == "not judged"
    assert (await live.judge(by_id["d4"], policy)).rationale == "not in the database"
    assert not (await deep.judge(by_id["d2"], policy)).alert
    assert (await best.judge(by_id["d3"], policy)).label == "passer_by", "deep fills the gap"
    other = await live.judge(by_id["d1"], PolicyText("old", "x"))
    assert other.abstained and other.rationale == "not judged", "verdicts match by policy name"


def test_a_services_policy_is_the_ground_truth_and_sets_score_as_one(
    place: tuple[Path, Path, Path],
) -> None:
    door, yard, _database = place
    sets = [load_dataset(door), load_dataset(yard)]
    alerting = ["touching_equipment", "person_at_house", "unknown_here"]
    sets = [with_policy(d, "home", "A house.", alerting, "judge") for d in sets]
    assert sets[0].labels.policies["home"].alert_on == ["touching_equipment", "person_at_house"]
    together = union(sets)
    assert sorted(together.labels.policies) == ["home", "old"]
    assert [e.id for e in together.scored_entries][:2] == ["door.d1", "door.d2"]
    assert together.case(together.scored_entries[-1]).media[0].is_file()
    assert together.labels.observed_hours == 48, "side by side: one calendar"


def test_union_needs_a_shared_policy(tmp_path: Path) -> None:
    a = clip_set(tmp_path / "a", "a", [("e1", "passer_by", "2026-01-14T10:00:00+00:00")])
    b = clip_set(tmp_path / "b", "b", [("e1", "passer_by", "2026-01-14T10:00:00+00:00")])
    data = yaml.safe_load((b / "labels.yaml").read_text())
    data["policies"] = {"old": {"description": "x", "alert_on": ["passer_by"]}}
    (b / "labels.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(DatasetError, match="share no policy"):
        union([load_dataset(a), load_dataset(b)])


def test_cli_scores_stored_verdicts_per_day_without_a_model(
    place: tuple[Path, Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    door, yard, database = place
    config = tmp_path / "vidette.yaml"
    config.write_text(yaml.safe_dump({
        "storage": {"media_dir": str(tmp_path / "m"), "database": str(database)},
        "policies": [POLICY],
    }))  # fmt: skip
    before = database.stat().st_mtime_ns
    args = ["eval", str(door), str(yard), "--config", str(config), "--policy-from-config",
            "home", "--judge", "stored-live", "--judge", "stored-deep",
            "--judge", "always"]  # fmt: skip
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "Dataset  door+yard: 5 entries scored" in out
    assert "Truth    2 of 5 entries should alert" in out
    assert "by day: FP / FN" in out and "2026-01-14" in out
    assert "not in the database 1" in out and "not judged 1" in out
    assert database.stat().st_mtime_ns == before, "read-only"
    assert main([*args, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    live = next(r for r in data["runs"] if r["judge"] == "stored-live")
    assert (live["tp"], live["fp"], live["fn"]) == (1, 1, 1)
    assert live["by_day"][0] == {"day": "2026-01-14", "entries": 2, "tp": 1, "fp": 1, "fn": 0,
                                 "abstained": 0}  # fmt: skip
    assert live["vlm"]["calls"] == 0
    deep = next(r for r in data["runs"] if r["judge"] == "stored-deep")
    assert (deep["tp"], deep["fp"], deep["fn"]) == (1, 0, 1)
    assert main(["eval", str(door), "--config", str(config), "--policy-from-config", "nope"]) == 2
    assert "no such policy" in capsys.readouterr().err
