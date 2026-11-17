"""Calibration (ADR-0020): 👍/👎 move a scenario on a camera a step quieter or back,
never past the floor of a protected scenario; every step stored, announced and taken
back."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_telegram import ALERTS, LEVELS, OWNER, Bot, alert, bot, callback, command, event

import vidette.auth.deps as auth_deps
from vidette.auth.service import ANONYMOUS_ADMIN, Principal
from vidette.calibration.rules import (
    KeyState,
    Mark,
    Rules,
    effect,
    marks_from,
    propose,
    state_from,
)
from vidette.calibration.service import Calibrator
from vidette.core.config import PolicyConfig, VidetteConfig, validate_config_text
from vidette.db import Database, read_calibration
from vidette.verdicts.base import Verdict

HOME = PolicyConfig.model_validate({"name": "home", "description": "A house.", "levels": LEVELS})
POLICIES = {"home": HOME}
RULES = Rules()
DOOR = ("home", "person_at_house", "front_door")
PANEL = ("home", "touching_equipment", "front_door")
NOW = 1_800_000_000.0


def down(
    n: int, key: tuple[str, str, str] = DOOR, reason: str | None = "passing", **kw: Any
) -> Mark:
    at = NOW - 3600 + n
    return Mark(f"ev-{key[1]}-{n}", key, "down", at, level=_level(key), reason=reason, **kw)


def up(n: int, key: tuple[str, str, str] = DOOR, **kw: Any) -> Mark:
    return Mark(f"ev-up-{n}", key, "up", NOW - 1800 + n, level=_level(key), **kw)


def _level(key: tuple[str, str, str]) -> str:
    found = HOME.scenario_level(key[1])
    assert found is not None
    return found[0]


# --- the rule -----------------------------------------------------------------------------


def test_four_false_alarms_and_no_up_raise_the_bar_then_the_digest() -> None:
    assert propose([down(n) for n in range(3)], {}, RULES, POLICIES, now=NOW) == []
    [first] = propose([down(n) for n in range(4)], {}, RULES, POLICIES, now=NOW)
    assert (first.step_from, first.step_to, first.bar) == (0, 1, 0.9)
    assert first.evidence["down"] == 4 and first.evidence["reasons"] == {"passing": 4}
    after_bar = {DOOR: KeyState(step=1, bar=0.9, step_id=1, since=NOW - 3000, active=(1,))}
    assert propose([down(n) for n in range(8)], after_bar, RULES, POLICIES, now=NOW) == [], (
        "marks before the last change count once"
    )
    later = [Mark(f"late-{n}", DOOR, "down", NOW - 100 + n, level="elevated") for n in range(4)]
    [second] = propose(later, after_bar, RULES, POLICIES, now=NOW)
    assert (second.step_from, second.step_to, second.bar) == (1, 2, None)
    top = {DOOR: KeyState(step=2, step_id=2, since=NOW - 3000, active=(1, 2))}
    assert propose(later, top, RULES, POLICIES, now=NOW) == [], "the digest is the last step"


def test_one_up_blocks_and_some_reasons_do_not_count() -> None:
    assert propose([*(down(n) for n in range(5)), up(1)], {}, RULES, POLICIES, now=NOW) == []
    mixed = [down(0, reason="stalled"), down(1, reason="camera"), down(2, reason="ours"),
             down(3), down(4), down(5)]  # fmt: skip
    assert propose(mixed, {}, RULES, POLICIES, now=NOW) == [], (
        "a frozen recording, a wrong camera and our own people are not the policy's fault"
    )
    [step] = propose([*mixed, down(6, reason=None)], {}, RULES, POLICIES, now=NOW)
    assert step.evidence["reasons"] == {"passing": 3, "none": 1}
    old = [Mark(f"old-{n}", DOOR, "down", NOW - 15 * 86400, level="elevated") for n in range(9)]
    assert propose(old, {}, RULES, POLICIES, now=NOW) == [], "older than the window"


def test_right_but_unimportant_skips_the_bar() -> None:
    marks = [down(n, reason="unimportant") for n in range(3)] + [down(3)]
    [step] = propose(marks, {}, RULES, POLICIES, now=NOW)
    assert (step.step_to, step.bar) == (2, None), "the judge saw right: only the digest helps"


def test_protected_scenarios_get_a_capped_bar_and_never_the_digest() -> None:
    marks = [down(n, PANEL, reason="unimportant") for n in range(6)]
    [step] = propose(marks, {}, RULES, POLICIES, now=NOW)
    assert (step.step_to, step.bar) == (1, 0.75), "wake: always: at most the protected bar"
    capped = {PANEL: KeyState(step=1, bar=0.75, step_id=1, since=NOW - 7200, active=(1,))}
    assert propose(marks, capped, RULES, POLICIES, now=NOW) == [], "never further"
    relaxed = PolicyConfig.model_validate(
        {"name": "home", "description": "x", "sensitivity": "relaxed", "levels": LEVELS}
    )
    assert propose(marks, {}, RULES, {"home": relaxed}, now=NOW) == [], (
        "relaxed already asks for 0.75: nothing stricter is allowed"
    )
    named = Rules(protect=frozenset({"elevated"}))
    [door] = propose([down(n, reason="unimportant") for n in range(4)], {}, named, POLICIES,
                     now=NOW)  # fmt: skip
    assert (door.step_to, door.bar) == (1, 0.75), "calibration.protect names a level"
    # a digest step made before the scenario was protected is capped where it is applied
    stale = KeyState(step=2, step_id=9, since=0, active=(9,))
    found = effect(stale, HOME, "touching_equipment", level="critical", confidence=0.9,
                   rules=RULES)  # fmt: skip
    assert found is None, "0.9 is above the protected bar: it wakes"
    found = effect(stale, HOME, "touching_equipment", level="critical", confidence=0.6,
                   rules=RULES)  # fmt: skip
    assert found is not None and (found.step, found.bar) == (1, 0.75)


def test_effect_bar_digest_and_doubt() -> None:
    bar = KeyState(step=1, bar=0.9, step_id=3, since=0, active=(3,))
    assert effect(bar, HOME, "person_at_house", level="elevated", confidence=0.95,
                  rules=RULES) is None  # fmt: skip
    low = effect(bar, HOME, "person_at_house", level="elevated", confidence=0.85, rules=RULES)
    assert low is not None and low.record() == {"step": 3, "level": 1, "bar": 0.9,
                                                "route": "digest"}  # fmt: skip
    assert effect(bar, HOME, "person_at_house", level="elevated", confidence=None,
                  rules=RULES) is None, "no number to compare: it wakes"  # fmt: skip
    digest = KeyState(step=2, step_id=4, since=0, active=(3, 4))
    assert effect(digest, HOME, "person_at_house", level="elevated", confidence=0.99,
                  rules=RULES) is not None  # fmt: skip
    assert effect(digest, HOME, "passer_by", level="critical", confidence=0.9,
                  rules=RULES) is None, "raised by recurrence: left alone"  # fmt: skip


def test_an_up_on_what_a_step_hid_takes_it_back_at_once() -> None:
    states = {DOOR: KeyState(step=2, step_id=7, since=NOW - 7200, active=(5, 7))}
    [back] = propose([up(1, hidden_by=7), up(2, hidden_by=7)], states, RULES, POLICIES, now=NOW)
    assert back.undo == 7 and back.step_to == 0
    assert propose([up(1, hidden_by=99)], states, RULES, POLICIES, now=NOW) == [], (
        "a step no longer in force"
    )


def test_marks_come_from_confirmed_events_only(tmp_path: Path) -> None:
    rows = [
        _row("a", "confirmed", "down", "person_at_house", reason="passing"),
        _row("b", "dismissed", "down", "passer_by"),
        _row("c", "confirmed", None, "person_at_house"),
        _row("d", "confirmed", "up", "person_at_house", calibration={"step": 4, "route": "digest"}),
    ]
    marks = marks_from(rows)
    assert [(m.event_id, m.answer, m.reason, m.hidden_by) for m in marks] == [
        ("a", "down", "passing", None),
        ("d", "up", None, 4),
    ]


def _row(event_id: str, state: str, feedback: str | None, label: str, *, reason: str | None = None,
         calibration: dict[str, Any] | None = None, at: float = NOW, confidence: float = 0.85,
         people: dict[str, Any] | None = None, camera: str = "front_door") -> Any:  # fmt: skip
    from vidette.db import EventRow

    level = (HOME.scenario_level(label) or ("none",))[0]
    intent: dict[str, Any] = {"verdicts": [{
        "policy": "home", "alert": state == "confirmed", "abstained": False, "label": label,
        "level": level, "persons": 1, "confidence": confidence,
    }]}  # fmt: skip
    if reason is not None or feedback is not None:
        intent["review"] = {"answer": "wrong" if feedback == "down" else "right", "at": at + 30}
        if reason:
            intent["review"]["reason"] = reason
    if calibration is not None:
        intent["calibration"] = calibration
    if people is not None:
        intent["people"] = people
    return EventRow(
        id=event_id, camera=camera, started_at=at, ended_at=at + 10, state=state, kinds=[],
        zones=[], geometry={}, summary=None, intent=intent,
        policy="home" if state == "confirmed" else None, feedback=feedback,
        snapshot_path=None, clip_path=None,
    )  # fmt: skip


# --- config ---------------------------------------------------------------------------------


def test_config_checks_protect_names_and_the_bars() -> None:
    base = "policies:\n  - name: home\n    description: x\n    levels:\n      top:\n        " \
           "wake: always\n        scenarios: {panel: touches the panel}\n"  # fmt: skip
    assert validate_config_text(base + "calibration: {protect: [panel, top]}\n").valid
    report = validate_config_text(base + "calibration: {protect: [panle]}\n")
    assert not report.valid and "panle" in report.errors[0]
    report = validate_config_text(base + "calibration: {bar: 0.6, protected_bar: 0.8}\n")
    assert not report.valid and "protected_bar" in report.errors[0]
    assert VidetteConfig().calibration.enabled, "on by default: every step is said and undone"


# --- the service, the worker and the bot ----------------------------------------------------


async def calibrated_bot(tmp_path: Path, **calibration: Any) -> tuple[Bot, Calibrator]:
    b = await bot(tmp_path, recipients="all")
    config = b.config.model_copy(
        update={"calibration": b.config.calibration.model_copy(update=calibration)}
    )
    calibrator = Calibrator(config, b.db, clock=lambda: b.clock[0])
    b.service.calibration = calibrator
    calibrator.announce = b.service.announce_calibration
    return b, calibrator


async def door_alert(b: Bot, clip: str, *, confidence: float = 0.85) -> Any:
    event_id = await event(b, clip, scenario="person_at_house", level="elevated",
                           persons=1, started=b.clock[0] - 60)  # fmt: skip
    row = await b.db.get_event(event_id)
    assert row is not None and row.intent is not None
    intent = dict(row.intent)
    intent["verdicts"] = [{**intent["verdicts"][0], "confidence": confidence}]
    await b.db.update_event(event_id, intent=intent)
    return event_id


async def test_marks_in_telegram_make_a_step_say_it_and_take_it_back(tmp_path: Path) -> None:
    b, calibrator = await calibrated_bot(tmp_path)
    for n in range(4):
        b.clock[0] += 600  # apart: alerts within `collapse` fold into one message
        event_id = await door_alert(b, f"20260115-1015{n:02d}")
        [row] = [m for m in await alert(b, event_id) if m.chat_id == OWNER]
        await b.service.handle_update(callback(b, row, "no", update_id=10 + 2 * n))
        await b.service.handle_update(callback(b, row, "r:passing", update_id=11 + 2 * n))
    [step] = await b.db.calibration_steps()
    assert (step.key, step.step_to, step.bar) == (DOOR, 1, 0.9)
    said = [c for c in b.fake.all("sendMessage") if "🎚" in c.params["text"]]
    assert len(said) == 2, "one line to each recipient"
    text = said[0].params["text"]
    assert text == (
        "🎚 person at house (Дверь) теперь будит, только если судья уверен на 90 % "
        "и больше: 4 раза 👎 «прошёл мимо»."
    )
    assert said[0].params["disable_notification"] is True
    assert said[0].params["message_thread_id"] == ALERTS
    [[undo]] = said[0].params["reply_markup"]["inline_keyboard"]
    assert undo["callback_data"] == f"c:{step.id}:undo"

    b.clock[0] += 600
    low = await door_alert(b, "20260115-103000", confidence=0.8)
    row = await b.db.get_event(low)
    assert row is not None
    found = await calibrator.effect(HOME, (row.intent or {})["verdicts"][0], "front_door")
    assert found is not None and found.step_id == step.id

    tuning = command("/tuning")
    tuning["message"]["date"] = int(b.clock[0])  # the bot ignores commands older than 10 min
    await b.service.handle_update(tuning)
    listing = b.fake.last("sendMessage").params
    assert (
        "Что изменили ваши 👍/👎" in listing["text"]
        and "только от 90 % уверенности" in (listing["text"])
    )
    press = {
        "update_id": 99,
        "callback_query": {
            "id": "cb99", "from": {"id": OWNER}, "data": f"c:{step.id}:undo",
            "message": {"message_id": 4242, "chat": {"id": OWNER}},
        },
    }  # fmt: skip
    await b.service.handle_update(press)
    assert b.fake.last("answerCallbackQuery").params["text"] == "Отменено"
    assert "Отменено" in b.fake.last("editMessageText").params["text"]
    [taken] = await b.db.calibration_steps()
    assert taken.reverted_by == "telegram" and not taken.active
    await b.service.handle_update({**press, "update_id": 100})
    assert b.fake.last("answerCallbackQuery").params["text"] == "Уже отменено"
    await b.db.close()


async def test_a_step_sends_new_alerts_to_the_digest_and_an_up_there_undoes_it(
    tmp_path: Path,
) -> None:
    b, calibrator = await calibrated_bot(tmp_path)
    await b.db.add_calibration_step(DOOR, 0, 2, bar=None, evidence={"down": 4, "reasons": {}})
    event_id = await door_alert(b, "20260115-101500", confidence=0.95)
    row = await b.db.get_event(event_id)
    assert row is not None
    found = await calibrator.effect(HOME, (row.intent or {})["verdicts"][0], "front_door")
    assert found is not None
    intent = dict(row.intent or {})
    intent["calibration"] = found.record()
    await b.db.update_event(event_id, intent=intent)
    assert await alert(b, event_id) == [], "confirmed, but for the digest"
    day = await b.service.day(OWNER)
    assert [item.title for item in day.notable] == ["внимание · в сводке по вашим отметкам"]

    await b.db.set_event_feedback(event_id, "up")
    [change] = await calibrator.review()
    assert change.kind == "restored"
    assert "снова будит" in b.fake.last("sendMessage").params["text"]
    [step] = await b.db.calibration_steps()
    assert step.reverted_by == "mark"
    await b.db.close()


async def test_the_worker_keeps_a_calibrated_alert_for_the_digest(tmp_path: Path) -> None:
    from test_verdicts_worker import ScriptedJudge

    from vidette.core.events import InProcessEventBus
    from vidette.verdicts.worker import VerdictWorker

    b, calibrator = await calibrated_bot(tmp_path)
    await b.db.add_calibration_step(DOOR, 0, 1, bar=0.9, evidence={"down": 4})
    verdict = Verdict(alert=True, label="person_at_house", level="elevated", confidence=0.8,
                      persons=1)  # fmt: skip

    async def emit(_kind: str, _payload: dict[str, Any]) -> None:
        return None

    worker = VerdictWorker(
        b.config, InProcessEventBus(), b.db, emit=emit, backend=_local,
        judges=lambda _backend: ScriptedJudge(verdict), media_dir=tmp_path / "media",
        calibration=calibrator, clock=lambda: b.clock[0],
    )  # fmt: skip
    from vidette.core.incidents import Incident

    event_id = await event(b, "20260115-101500", state="observed", scenario=None, level=None)
    row = await b.db.get_event(event_id)
    assert row is not None and row.source_ref is not None
    incident = Incident(source="clip-folder", source_id=row.source_ref, camera="front_door",
                        started_at=b.clock[0] - 70, ended_at=b.clock[0] - 60)  # fmt: skip
    judged = await worker.handle(incident)
    assert judged is not None and judged.state == "confirmed"
    assert (judged.intent or {})["calibration"]["route"] == "digest"
    assert await alert(b, event_id) == []
    await b.db.close()


async def _local() -> Any:
    from vidette.core.config import VlmBackend

    return VlmBackend.local


async def test_steps_are_stored_and_taken_back_with_the_later_ones(db: Database) -> None:
    first = await db.add_calibration_step(DOOR, 0, 1, bar=0.9, evidence={"down": 4})
    second = await db.add_calibration_step(DOOR, 1, 2, bar=None, evidence={"down": 4})
    other = await db.add_calibration_step(PANEL, 0, 1, bar=0.75, evidence={"down": 4})
    states = state_from(await db.calibration_steps())
    assert states[DOOR].step == 2 and states[DOOR].active == (first.id, second.id)
    taken = await db.revert_calibration_step(first.id, "web")
    assert [r.id for r in taken] == [first.id, second.id]
    states = state_from(await db.calibration_steps())
    assert states[DOOR].step == 0 and states[PANEL].step == 1 and other.active
    assert await db.revert_calibration_step(first.id, "web") == [], "only once"
    assert [r.reverted_by for r in read_calibration(db.path)] == ["web", "web", None]


# --- the web page ---------------------------------------------------------------------------


async def test_the_api_lists_steps_and_takes_one_back(tmp_path: Path) -> None:
    from vidette.api.routers.calibration import router
    from vidette.runtime import AppRuntime

    b, _calibrator = await calibrated_bot(tmp_path)
    await b.db.close()
    runtime = AppRuntime(b.config)
    await runtime.db.connect()
    evidence = {"down": 4, "reasons": {"passing": 4}}
    step = await runtime.db.add_calibration_step(DOOR, 0, 1, bar=0.9, evidence=evidence)
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = runtime

    async def admin() -> Principal:
        return ANONYMOUS_ADMIN

    app.dependency_overrides[auth_deps.current_principal] = admin
    with TestClient(app) as client:
        listed = client.get("/api/v1/calibration").json()
        assert listed["rule"]["marks"] == 4 and "touching_equipment" in listed["rule"]["protected"]
        [shown] = listed["steps"]
        assert shown["in_force"] and shown["camera_name"] == "Дверь"
        assert shown["scenario_title"] == "person at house"
        assert client.post(f"/api/v1/calibration/{step.id}/revert").json() == {
            "reverted": [step.id]
        }
        assert client.post(f"/api/v1/calibration/{step.id}/revert").status_code == 404
        assert client.get("/api/v1/calibration").json()["steps"][0]["reverted_by"] == "web"
    await runtime.db.close()
