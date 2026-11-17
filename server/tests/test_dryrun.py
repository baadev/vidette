"""`vidette policy dry-run` (docs/calibration.md): stored verdicts of the last days routed
again through a policy and calibration (off, the steps in force, or replayed from the
marks), per day and per scenario, read-only, nothing sent, no model called."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from test_calibration import HOME, NOW, _row

from vidette.__main__ import main
from vidette.calibration.dryrun import render_text, replay, route_verdicts
from vidette.core.config import Sensitivity, VidetteConfig
from vidette.db import Database, read_events


def _config(**calibration: Any) -> VidetteConfig:
    return VidetteConfig.model_validate(
        {"policies": [HOME.model_dump(mode="json")], "calibration": calibration}
    )


def test_routing_from_stored_verdicts() -> None:
    record = {"policy": "home", "label": "person_at_house", "level": "elevated",
              "confidence": 0.9, "persons": 1, "abstained": False}  # fmt: skip
    assert route_verdicts([record], [HOME], None).route == "wake"
    known = {"seen": [{"n": 1, "status": "known", "person": "sam"}]}
    assert route_verdicts([record], [HOME], known).route == "digest", "unless_known, all ours"
    assert route_verdicts([{**record, "label": "passer_by"}], [HOME], None).route == "silent"
    assert route_verdicts([{**record, "abstained": True}], [HOME], None).route == "unjudged"
    assert route_verdicts([], [HOME], None).route == "unjudged"
    relaxed = HOME.model_copy(update={"sensitivity": Sensitivity.relaxed})
    assert route_verdicts([{**record, "confidence": 0.6}], [relaxed], None).route == "silent"


def test_the_dry_run_replays_calibration_from_the_marks() -> None:
    day = 86400.0
    rows = []
    for n in range(4):  # four false alarms on day 1, marked an hour later
        rows.append(_row(f"d{n}", "confirmed", "down", "person_at_house", reason="passing",
                         at=NOW - 5 * day + n * 60))  # fmt: skip
    rows.append(_row("later-low", "confirmed", None, "person_at_house", at=NOW - 2 * day,
                     confidence=0.8))  # fmt: skip
    rows.append(_row("later-high", "confirmed", None, "person_at_house", at=NOW - day,
                     confidence=0.95))  # fmt: skip
    rows.append(_row("panel", "confirmed", None, "touching_equipment", at=NOW - day,
                     confidence=0.6))  # fmt: skip
    config = _config()
    off = replay(rows, config, since=NOW - 7 * day, until=NOW, mode="off")
    assert [e.route for e in off.events].count("wake") == 7
    run = replay(rows, config, since=NOW - 7 * day, until=NOW, mode="replay",
                 alerted={"d0"})  # fmt: skip
    routes = {e.event_id: e.route for e in run.events}
    assert routes["later-low"] == "digest" and routes["later-high"] == "wake"
    assert routes["panel"] == "wake", "another scenario, untouched"
    [step] = run.steps
    assert (step.scenario, step.step_to, step.bar) == ("person_at_house", 1, 0.9)
    assert [e.event_id for e in run.moved()] == ["later-low"]
    text = render_text(run, camera_names={"front_door": "Front door"})
    assert "Calibration moved 1 alert(s) to the digest." in text
    assert "person_at_house on Front door: wakes only from 90 % sure" in text
    totals = next(line for line in text.splitlines() if line.startswith("total"))
    assert totals.split() == ["total", "7", "6", "7", "1", "0", "0", "1", "4/0"]
    active = replay(rows, config, since=NOW - 7 * day, until=NOW, mode="active", steps=run.steps)
    assert {e.event_id for e in active.moved()} == {"d0", "d1", "d2", "d3", "later-low"}, (
        "active: today's steps as if they had held all week, the marked ones included"
    )


def test_the_cli_dry_run_reads_the_database_read_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncio

    config_path = tmp_path / "vidette.yaml"
    import yaml

    config_path.write_text(yaml.safe_dump({
        "storage": {"media_dir": str(tmp_path / "m"), "database": str(tmp_path / "v.db")},
        "policies": [HOME.model_dump(mode="json")],
    }))  # fmt: skip

    async def seed() -> None:
        db = Database(tmp_path / "v.db")
        await db.connect()
        now = time.time()
        for n in range(5):
            row = _row(f"e{n}", "confirmed", "down", "person_at_house", reason="passing",
                       at=now - 3 * 86400 + n * 600)  # fmt: skip
            await db.insert_event(
                row.id, row.camera, row.started_at, row.state, [], [], {},
                ended_at=row.ended_at, intent=row.intent, policy="home",
            )  # fmt: skip
            await db.set_event_feedback(row.id, "down")
        await db.close()

    asyncio.run(seed())
    before = (tmp_path / "v.db").stat().st_mtime_ns
    assert main(["policy", "--config", str(config_path), "dry-run", "--days", "7"]) == 0
    out = capsys.readouterr().out
    assert "Calibration moved 1 alert(s) to the digest." in out
    assert (tmp_path / "v.db").stat().st_mtime_ns == before, "read-only"
    assert len(read_events(tmp_path / "v.db")) == 5
    assert main(["policy", "--config", str(config_path), "calibration"]) == 0
    assert "no calibration steps yet" in capsys.readouterr().out
    proposed = tmp_path / "proposed.yaml"
    quieter = HOME.model_dump(mode="json")
    quieter["levels"]["elevated"]["wake"] = "never"
    proposed.write_text(yaml.safe_dump({"policies": [quieter]}))
    assert main(["policy", "--config", str(config_path), "dry-run", "--calibration", "off",
                 "--policy-file", str(proposed), "--json"]) == 0  # fmt: skip
    import json

    data = json.loads(capsys.readouterr().out)
    assert sum(day.get("wake", 0) for day in data["days"].values()) == 0
    assert sum(day.get("silent", 0) for day in data["days"].values()) == 5


def test_the_dry_run_counts_what_the_pre_filter_skipped() -> None:
    from dataclasses import replace

    judged = _row("judged", "dismissed", None, "passer_by", at=NOW - 3600)
    skipped = _row("skipped", "observed", None, "passer_by", at=NOW - 1800)
    rows = [
        replace(judged, intent={**(judged.intent or {}), "triage": {"class": "now"}}),
        replace(skipped, intent={"triage": {"class": "skip", "reason": "tiny"}}),
    ]
    run = replay(rows, _config(), since=NOW - 86400, until=NOW, mode="off")
    assert run.triaged and [e.triage for e in run.events] == ["now", "skip"]
    assert [e.route for e in run.events] == ["silent", "unjudged"]
    text = render_text(run)
    assert "1 judged first, 0 when the model was idle, 1 skipped (nobody there)" in text
