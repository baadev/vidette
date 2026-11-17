"""What happened across cameras (ADR-0022): the links of an alert to the other cameras,
incident mode in serve, what the alert and the digest say, and the assistant's answers."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from test_incident_policy import POLICY

from vidette.core.config import VidetteConfig
from vidette.core.events import InProcessEventBus
from vidette.db import Database, EventRow
from vidette.incident.link import find_links
from vidette.incident.look import Look, similarity
from vidette.incident.mode import read_incident
from vidette.incident.service import IncidentService
from vidette.triage.track import Sighting

T0 = time.time() - 600



def config(tmp_path: Path, **incident: Any) -> VidetteConfig:
    return VidetteConfig.model_validate(
        {
            "storage": {"media_dir": str(tmp_path / "media"), "database": str(tmp_path / "v.db")},
            "clip_folders": [
                {"path": str(tmp_path / "a"), "camera": "door"},
                {"path": str(tmp_path / "b"), "camera": "street"},
                {"path": str(tmp_path / "c"), "camera": "yard"},
            ],
            "policies": [POLICY],
            "incident": incident,
        }
    )


# --- links to the other cameras -----------------------------------------------------------


async def an_event(
    db: Database,
    event_id: str,
    camera: str,
    at: float,
    *,
    state: str = "dismissed",
    label: str = "passer_by",
    level: str | None = "none",
    identity: int | None = None,
    clip: str | None = None,
    person: float = 0.9,
    persons: int = 1,
) -> EventRow:
    seen = []
    if identity is not None:
        seen.append({"n": 1, "status": "unknown", "score": 0.2, "identity": {"id": identity}})
    intent = {
        "verdicts": [{"policy": "home", "label": label, "level": level, "persons": persons,
                      "alert": state == "confirmed", "rationale": f"{camera}: {label}"}],
        "people": {"version": 1, "checked_at": "2026-11-17T19:00:00+02:00", "engine": "t",
                   "threshold": 0.4, "enrolled": 0, "seen": seen},
        "timeline": {"person_seen": at + 1},
        "triage": {"facts": {"person": person}},
    }  # fmt: skip
    await db.insert_event(
        event_id, camera, at, state, [], [], {}, ended_at=at + 30, source="clip-folder",
        source_ref=event_id, clip_path=clip, intent=intent, policy="home",
    )  # fmt: skip
    row = await db.get_event(event_id)
    assert row is not None
    return row


LOOKS = {
    "dark": Look(hue=(), value=(0.7, 0.2, 0.1, 0, 0, 0), grey=False, frames=2),
    "dark2": Look(hue=(), value=(0.68, 0.22, 0.1, 0, 0, 0), grey=False, frames=2),
    "white": Look(hue=(), value=(0, 0, 0, 0.1, 0.2, 0.7), grey=False, frames=2),
}


async def test_the_strongest_evidence_links_each_other_cameras_event(tmp_path: Path) -> None:
    cfg = config(tmp_path, look=True, look_threshold=0.9)
    db = Database(cfg.storage.database)
    await db.connect()
    alert = await an_event(db, "street", "street", T0 + 60, state="confirmed",
                           label="door_or_gate_attempt", level="critical", identity=7)
    face = await an_event(db, "yard", "yard", T0 + 30, identity=7)
    look = await an_event(db, "door", "door", T0)
    other = await an_event(db, "door2", "door", T0 + 20)
    nobody = await an_event(db, "door3", "door", T0 + 40, person=0.0, persons=0)
    far = await an_event(db, "yard-far", "yard", T0 - 900)
    rows = [alert, face, look, other, nobody, far]
    looks = {"street": "dark", "door": "dark2", "door2": "white"}

    async def sightings(_row: EventRow) -> list[Sighting]:
        return []

    async def look_for(row: EventRow, _s: list[Sighting]) -> Look | None:
        return LOOKS[looks[row.id]] if row.id in looks else None

    links = await find_links(alert, rows, cfg, sightings_for=sightings, look_for=look_for)
    assert [(link.event_id, link.evidence) for link in links] == [
        ("yard", "face"), ("door", "look"), ("door2", "time")
    ], "no person, or minutes away: not linked"
    assert links[1].score is not None and links[1].score >= 0.9
    assert links[1].gap_s == pytest.approx(-60.0)
    assert similarity(LOOKS["dark"], LOOKS["white"]) < 0.5
    plain = await find_links(alert, rows, config(tmp_path), sightings_for=sightings,
                             look_for=look_for)  # fmt: skip
    assert [link.evidence for link in plain] == ["face", "time", "time"], "off by default"
    await db.close()


async def test_neighbours_limit_the_clothes_cue(tmp_path: Path) -> None:
    cfg = config(tmp_path, neighbours={"street": ["yard"]})
    assert cfg.incident.neighbours_of("yard", "street") and not cfg.incident.neighbours_of(
        "street", "door"
    )
    with pytest.raises(ValueError, match=r"incident\.neighbours"):
        config(tmp_path, neighbours={"street": ["not a camera!"]})


async def test_an_alert_starts_incident_mode_and_stores_its_links(tmp_path: Path) -> None:
    cfg = config(tmp_path, look=False)
    db = Database(cfg.storage.database)
    await db.connect()
    now = time.time()
    alert = await an_event(db, "street", "street", now - 30, state="confirmed",
                           label="door_or_gate_attempt", level="critical", identity=7)
    await an_event(db, "yard", "yard", now - 90, identity=7)
    service = IncidentService(cfg, db, InProcessEventBus(), clock=lambda: now)
    first, second = await asyncio.gather(service.for_alert(alert), service.for_alert(alert))
    assert first is second and first is not None, "the work is done once per alert"
    assert [link.evidence for link in first.links] == ["face"]
    stored = await db.get_event("street")
    assert stored is not None and stored.intent is not None
    assert stored.intent["incident"]["role"] == "alert"
    assert stored.intent["incident"]["links"][0]["event_id"] == "yard"
    linked = await db.get_event("yard")
    assert linked is not None and linked.intent is not None
    record = linked.intent["incident"]
    assert record["role"] == "linked" and record["alert"] == "street" and record["reopened"]
    mode = read_incident(service.mode_path)
    assert mode is not None and mode.event_id == "street" and mode.until == now + 600
    assert service.active(now + 60) is not None and service.active(now + 700) is None
    context = service.context("yard", now + 60)
    assert context is not None and "camera street alerted (door_or_gate_attempt)" in context
    await db.close()


async def test_an_alert_a_calibration_step_quietened_starts_no_incident_mode(
    tmp_path: Path,
) -> None:
    cfg = config(tmp_path, look=False)
    db = Database(cfg.storage.database)
    await db.connect()
    alert = await an_event(db, "street", "street", time.time(), state="confirmed",
                           label="person_at_house", level="elevated")  # fmt: skip
    intent = dict(alert.intent or {}, calibration={"step": 1})
    await db.update_event("street", intent=intent)
    service = IncidentService(cfg, db, InProcessEventBus())
    await service.for_alert(await db.get_event("street"))  # type: ignore[arg-type]
    assert read_incident(service.mode_path) is None
    await db.close()


# --- the bot and the assistant ------------------------------------------------------------------


async def test_the_alert_says_where_else_the_same_person_was_and_sends_that_clip(
    tmp_path: Path,
) -> None:
    from test_telegram import alert, bot, event

    b = await bot(tmp_path)
    linked_id = await event(b, "20260115-101400", state="dismissed", scenario="passer_by",
                            level="none")  # fmt: skip
    event_id = await event(b, "20260115-101500")
    row = await b.db.get_event(event_id)
    linked_row = await b.db.get_event(linked_id)
    assert row is not None and linked_row is not None
    record = {"event_id": linked_id, "camera": "front_door", "at": linked_row.started_at,
              "gap_s": -60.0, "evidence": "look", "score": 0.91, "state": "dismissed",
              "label": "passer_by", "level": "none", "rationale": "Человек у стены дома.",
              "approximate": True}  # fmt: skip
    nearby = dict(record, event_id="other", evidence="time", gap_s=-100.0,
                  rationale="Кто-то проходит мимо.")  # fmt: skip
    intent = dict(row.intent or {},
                  incident={"id": "inc", "role": "alert", "links": [record, nearby]})  # fmt: skip
    await b.db.update_event(event_id, intent=intent)
    await alert(b, event_id)
    text = b.fake.all("sendMessage")[0].params["text"]
    assert "Похоже, тот же человек (одежда и время)" in text and "около" in text
    assert "Кто-то проходит мимо" not in text, "only the same person when there is one"
    assert "Человек у стены дома" in text and "судья это пропустил" in text
    captions = [c.params.get("caption") or "" for c in b.fake.calls if c.method == "sendPhoto"]
    assert any("Похоже, тот же человек" in c for c in captions), "its best frame follows"
    await b.db.close()


def test_the_incident_section_parses_and_defaults() -> None:
    cfg = VidetteConfig.model_validate({})
    assert cfg.incident.enabled and cfg.incident.seconds("keep_for") == 600
    assert cfg.understanding.windows.seconds("long_clip") == 30
    assert json.loads(json.dumps(cfg.incident.model_dump(mode="json")))["link_within"] == "3m"
