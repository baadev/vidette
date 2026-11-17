"""The Telegram bot (ADR-0013): policy levels, routing, alerts once per event, collapsing,
buttons that become labels, commands from allowed chats only, digests. A fake Bot API
stands in for Telegram; nothing here touches the network."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fakes.faces import FakeFaceEngine, photo
from fakes.openai_api import FakeVlmApi
from fakes.telegram_api import TOKEN, FakeTelegram
from test_clipfolder import collected_clip
from test_people import enrolled

from vidette.__main__ import main
from vidette.core.config import (
    VidetteConfig,
    VlmBackend,
    Wake,
    validate_config_text,
)
from vidette.core.events import EventState, InProcessEventBus
from vidette.core.incidents import Incident
from vidette.db import Database
from vidette.envfile import EnvFileError, load_env_file, parse_env_text
from vidette.people.check import build_report, crop_writer
from vidette.telegram.api import TelegramApi, TelegramError
from vidette.telegram.routing import decide, known_only
from vidette.telegram.service import TelegramService, unjudged_reason
from vidette.verdicts.base import Case, FrameSet, Verdict, policy_text
from vidette.verdicts.vlm import VlmJudge
from vidette.verdicts.worker import VerdictWorker, album_frames, early_may_alert
from vidette.vlm.budget import MemoryLedger
from vidette.vlm.client import VlmClient
from vidette.vlm.endpoint import endpoint_for

OWNER, MEMBER, STRANGER_CHAT = 1001, 1002, 9999
ALERTS, DIGEST = 11, 12

LEVELS = {
    "critical": {
        "title": "критично",
        "wake": "always",
        "scenarios": {
            "touching_equipment": "touches the electrical panel or a camera",
            "door_or_gate_attempt": "pulls or tries to open a door, a handle or a gate",
        },
    },
    "elevated": {
        "title": "внимание",
        "wake": "unless_known",
        "scenarios": {"person_at_house": "a person in the yard, at the gate or a door"},
    },
    "none": {
        "wake": "never",
        "scenarios": {
            "passer_by": {"description": "walks past", "title": "прохожие"},
            "household_member": "someone who lives here doing ordinary things",
        },
    },
}


def make_config(tmp_path: Path, **telegram: Any) -> VidetteConfig:
    dataset = tmp_path / "front-door"
    dataset.mkdir(exist_ok=True)
    tg: dict[str, Any] = {
        "token": TOKEN,
        "allowed_chats": f"{OWNER},{MEMBER}",
        "topics": f"{OWNER}:alerts={ALERTS},digest={DIGEST}",
        "recipients": "first",
        "names": True,
        "language": "ru",
        "camera_names": {"front_door": "Дверь"},
        "api_url": "http://telegram.test",
    }
    tg.update(telegram)
    return VidetteConfig.model_validate(
        {
            "storage": {"media_dir": str(tmp_path / "media"), "database": str(tmp_path / "v.db")},
            "clip_folders": [{"path": str(dataset), "camera": "front_door"}],
            "understanding": {
                "vlm": {"backend": "local", "language": "Russian", "local": {"model": "fake-vlm"}}
            },
            "policies": [
                {"name": "home", "description": "A private house.", "levels": LEVELS},
            ],
            "notifications": {"telegram": tg},
        }
    )


async def no_sleep(_seconds: float) -> None:
    return None


@dataclass
class Bot:
    service: TelegramService
    fake: FakeTelegram
    db: Database
    config: VidetteConfig
    root: Path
    clock: list[float] = field(default_factory=lambda: [time.time()])


async def bot(tmp_path: Path, *, store: Any = None, **telegram: Any) -> Bot:
    config = make_config(tmp_path, **telegram)
    clock = [time.time()]
    db = Database(config.storage.database, clock=lambda: clock[0])  # one clock for both
    await db.connect()
    fake = FakeTelegram()
    api = TelegramApi(TOKEN, base_url="http://telegram.test", transport=fake.transport(),
                      sleep=no_sleep)  # fmt: skip
    service = TelegramService(
        config, db, InProcessEventBus(), emit=_noop, api=api, people_store=store,
        face_engine=FakeFaceEngine(), clock=lambda: clock[0], sleep=no_sleep,
    )  # fmt: skip
    return Bot(service, fake, db, config, tmp_path / "front-door", clock)


async def _noop(_kind: str, _payload: dict[str, Any]) -> None:
    return None


async def event(
    b: Bot,
    clip_id: str,
    *,
    scenario: str | None = "touching_equipment",
    level: str | None = "critical",
    state: str = "confirmed",
    persons: int | None = 1,
    people: dict[str, Any] | None = None,
    started: float | None = None,
    summary: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    clip = collected_clip(b.root, clip_id)
    event_id = f"ev-{clip_id}"
    started = started if started is not None else b.clock[0] - 60
    verdict = {
        "policy": "home", "alert": state == "confirmed", "model_said_alert": True,
        "abstained": state == "observed", "label": scenario, "level": level,
        "persons": persons, "confidence": 0.9, "rationale": "Человек открывает щиток.",
        "model": "fake-vlm",
    }  # fmt: skip
    intent: dict[str, Any] = {"judge": "vlm-local", "backend": "local", "verdicts": [verdict]}
    if people is not None:
        intent["people"] = people
    intent.update(extra or {})
    await b.db.insert_event(
        event_id, "front_door", started, state, [], [], {}, ended_at=started + 8,
        summary=summary or verdict["rationale"], intent=intent,
        policy="home" if state == "confirmed" else None, clip_path=str(clip),
        source="clip-folder", source_ref=str(clip),
    )  # fmt: skip
    return event_id


def known_people(*statuses: str, names: tuple[str, ...] = ("Sam",)) -> dict[str, Any]:
    seen = []
    for n, status in enumerate(statuses, start=1):
        person = {"n": n, "status": status, "score": 0.7, "frames": [1], "crop": None}
        if status == "known":
            person.update(person="sam", name=names[0])
        seen.append(person)
    return {"version": 1, "checked_at": "2026-11-14T17:00:00+02:00", "engine": "fake",
            "threshold": 0.4, "enrolled": 1, "frames_checked": 4,
            "frames_with_readable_faces": 1, "unclear_frames": 0, "seen": seen}  # fmt: skip


async def alert(b: Bot, event_id: str) -> list[Any]:
    """The alert's text, then (awaited here) its frames and clip; the rows as they end."""
    row = await b.db.get_event(event_id)
    assert row is not None
    sent = await b.service.notify_event(row)
    await b.service.media_settled()
    return [await b.db.get_telegram_message(message.id) for message in sent]


def callback(
    b: Bot, row: Any, code: str, *, chat: int = OWNER, update_id: int = 1
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": chat},
            "data": f"a:{row.id}:{code}",
            "message": {"message_id": row.message_id, "chat": {"id": chat}},
        },
    }


def command(
    text: str, *, chat: int = OWNER, thread: int | None = None, age: float = 0
) -> dict[str, Any]:
    return {
        "update_id": 7,
        "message": {
            "message_id": 50, "date": int(time.time() - age), "text": text,
            "chat": {"id": chat}, "message_thread_id": thread,
        },
    }  # fmt: skip


# --- config ------------------------------------------------------------------------------


def test_config_reads_chats_and_topics_from_one_line_each(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    tg = config.notifications.telegram
    assert tg is not None
    assert tg.allowed_chats == [OWNER, MEMBER] and tg.recipient_chats == [OWNER]
    assert tg.topics[OWNER].alerts == ALERTS and tg.topics[OWNER].digest == DIGEST
    assert TOKEN not in repr(tg) and TOKEN not in repr(config)
    home = config.policies[0]
    assert home.wake_for("touching_equipment") is Wake.always
    assert home.wake_for("passer_by") is Wake.never and home.wake_for("other") is Wake.never


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"token": "not-a-token"}, "does not look like a bot token"),
        ({"topics": "1001 alerts 11"}, "entry 1 is not"),
        ({"topics": "5555:alerts=1"}, "not in allowed_chats"),
        ({"live": {"url": "http://192.0.2.10:1984"}}, "live view will be part of the Vidette app"),
        ({"digest_at": "25:00"}, "digest_at must be a local time"),
    ],
)
def test_config_mistakes_name_the_key_and_never_echo_the_token(
    tmp_path: Path, change: dict[str, Any], problem: str
) -> None:
    tg = {"token": TOKEN, "allowed_chats": "1001", **change}
    text = yaml.safe_dump({"notifications": {"telegram": tg}})
    report = validate_config_text(text)
    assert not report.valid and problem in " ".join(report.errors)
    assert TOKEN not in " ".join(report.errors)


def test_levels_are_checked(tmp_path: Path) -> None:
    def policy(levels: dict[str, Any]) -> str:
        return yaml.safe_dump({"policies": [{"name": "p", "description": "x", "levels": levels}]})

    twice = {"a": {"scenarios": {"x_y": "1"}}, "b": {"scenarios": {"x_y": "2"}}}
    assert "in two levels" in " ".join(validate_config_text(policy(twice)).errors)
    reserved = {"a": {"scenarios": {"other": "1"}}}
    assert "reserved" in " ".join(validate_config_text(policy(reserved)).errors)
    report = validate_config_text(policy({"a": {"wake": "unless_known", "scenarios": {"x": "1"}}}))
    assert report.valid and any("unless_known" in w for w in report.warnings)


def test_env_files_are_plain_assignments_not_shell(tmp_path: Path) -> None:
    text = "# secrets\nA=1\nTOPICS=1001:alerts=11,digest=12;1002:alerts=3\nexport B='two words'\n"
    assert parse_env_text(text) == {
        "A": "1", "TOPICS": "1001:alerts=11,digest=12;1002:alerts=3", "B": "two words",
    }  # fmt: skip
    path = tmp_path / "x.env"
    path.write_text("A=from-file\nC=3\n")
    path.chmod(0o644)
    env = {"A": "already"}
    applied, warnings = load_env_file(path, env)
    assert applied == ["C"] and env == {"A": "already", "C": "3"}
    assert warnings and "chmod 600" in warnings[0]
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert load_env_file(path, {})[1] == []
    path.write_text("A=1\nthis is not an assignment secret-value\n")
    with pytest.raises(EnvFileError) as caught:
        load_env_file(path, {})
    assert "secret-value" not in str(caught.value) and ":2:" in str(caught.value)


# --- routing -----------------------------------------------------------------------------


def test_who_wakes() -> None:
    sam, stranger = known_people("known"), known_people("known", "unknown")
    assert decide(Wake.always, sam, 1, quiet=True) == "alert"
    assert decide(Wake.never, None, None, quiet=False) == "digest"
    assert decide(Wake.unless_quiet, None, None, quiet=True) == "digest"
    assert decide(Wake.unless_known, sam, 1, quiet=False) == "digest"
    assert decide(Wake.unless_known, sam, 2, quiet=False) == "alert", "someone else was there"
    assert decide(Wake.unless_known, sam, None, quiet=False) == "alert", "the judge did not count"
    assert decide(Wake.unless_known, stranger, 2, quiet=False) == "alert"
    assert decide(Wake.unless_known, known_people(), 1, quiet=False) == "alert", "no clear face"
    assert decide(Wake.unless_known, None, 1, quiet=False) == "alert", "no face check"
    assert not known_only(None, 1)


def test_unjudged_reasons_are_said_plainly() -> None:
    assert unjudged_reason("no verdict: timeout: no answer in 400 s") == "судья не успел"
    assert unjudged_reason("no verdict: network: connection refused") == "судья недоступен"
    assert unjudged_reason(None) == "не дошла очередь"


# --- the VLM judge with levels -----------------------------------------------------------


async def test_a_policy_with_levels_asks_for_a_scenario_and_derives_the_alert(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    answers = iter(
        [
            {"scenario": "touching_equipment", "description": "Щиток.", "persons": 1,
             "confidence": 0.8},
            {"scenario": "passer_by", "description": "Мимо.", "persons": 2, "confidence": 0.9},
            {"scenario": "other", "description": "Пусто.", "persons": 0, "confidence": 0.9},
            {"scenario": "unsure", "description": "Темно.", "persons": 0, "confidence": 0.1},
            {"scenario": "touching_equipment", "description": "Наверное, щиток.", "persons": 1,
             "subject_frames": [], "confidence": 0.7},
            {"scenario": "touching_equipment", "description": "Щиток.", "persons": 1,
             "subject_frames": [1, 7], "confidence": 0.9},
        ]
    )  # fmt: skip
    fake = FakeVlmApi(answer=lambda _t, _i, _b: next(answers))
    http = httpx.AsyncClient(transport=fake.transport(), base_url="http://localhost:11434")
    client = VlmClient(endpoint_for(config, VlmBackend.local), ledger=MemoryLedger(), http=http)
    judge = VlmJudge(client, language="Russian")
    frame = tmp_path / "f.jpg"
    frame.write_bytes(bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9"))
    case = Case(Incident(source="t", source_id="1", camera="front_door", started_at=0),
                media=(frame,))  # fmt: skip
    text = policy_text(config.policies[0])
    critical = await judge.judge(case, text)
    assert critical.alert and critical.level == "critical" and critical.persons == 1
    assert critical.label == "touching_equipment" and critical.rationale == "Щиток."
    passer = await judge.judge(case, text)
    assert not passer.alert and passer.level == "none" and not passer.abstained
    other = await judge.judge(case, text)
    assert not other.alert and other.level is None and other.label == "other"
    unsure = await judge.judge(case, text)
    assert unsure.abstained and not unsure.alert
    assumed = await judge.judge(case, text)
    assert assumed.abstained and not assumed.alert, "an alert no frame shows is not one"
    assert assumed.rationale is not None and "pointed at no frame" in assumed.rationale
    shown = await judge.judge(case, text)
    assert shown.alert and shown.subject_at_s == [], "frame 1 has no known time here"
    body = fake.requests[0]["body"]
    schema = body["response_format"]["json_schema"]["schema"]
    assert "touching_equipment" in schema["properties"]["scenario"]["enum"]
    prompt = json.dumps(body["messages"], ensure_ascii=False)
    assert "Level critical (an alert)" in prompt and "in Russian" in prompt
    assert "subject_frames" in schema["required"] and "never a guessed gender" in prompt
    await http.aclose()


def test_early_windows_alert_only_for_levels_that_wake_for_anyone(tmp_path: Path) -> None:
    [policy] = make_config(tmp_path).policies
    critical = Verdict(alert=True, label="touching_equipment", level="critical")
    elevated = Verdict(alert=True, label="person_at_house", level="elevated")
    assert early_may_alert(policy, critical) and not early_may_alert(policy, elevated)
    plain = policy.model_copy(update={"levels": {}})
    assert early_may_alert(plain, Verdict(alert=True)), "a policy without levels: as before"


async def test_the_album_is_the_faces_best_moments_then_what_the_judge_pointed_at(
    tmp_path: Path,
) -> None:
    frames = []
    for n in (1, 2, 3):
        frame = tmp_path / "chosen" / f"{n}.jpg"
        frame.parent.mkdir(exist_ok=True)
        frame.write_bytes(b"\xff\xd8\xff\xd9")
        frames.append(frame)
    chosen = FrameSet(frames, [0.5, 2.0, 4.0], "activity", 6.0)
    verdict = Verdict(alert=True, label="person_at_house", subject_at_s=[4.0, 2.0])
    case = Case(Incident(source="t", source_id="1", camera="c", started_at=0), frames=chosen)
    album = await album_frames(case, chosen, verdict, None, tmp_path / "album")
    assert [(a["t"], a["why"]) for a in album] == [(4.0, "subject"), (2.0, "subject")]
    assert await album_frames(case, chosen, Verdict(alert=False), None, tmp_path) == []


async def test_the_worker_judges_the_newest_incident_first(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    now = [1000.0]

    async def backend() -> VlmBackend:
        return VlmBackend.local

    worker = VerdictWorker(
        config, InProcessEventBus(), Database(tmp_path / "x.db"), emit=_noop, backend=backend,
        judges=lambda _b: None,  # type: ignore[arg-type,return-value]
        media_dir=tmp_path, clock=lambda: now[0], max_age_s=900,
    )  # fmt: skip

    def incident(name: str, ended: float) -> Incident:
        return Incident(source="t", source_id=name, camera="front_door", started_at=ended - 5,
                        ended_at=ended)  # fmt: skip

    pending = {i.key: i for i in (incident("a", 10), incident("b", 600), incident("c", 400))}
    first = worker.next_incident(pending)
    assert first is not None and first.source_id == "b"
    assert worker.status.skipped_old == 1, "a ended 990 s ago: too old to judge"
    second = worker.next_incident(pending)
    assert second is not None and second.source_id == "c" and not pending


# --- alerts ------------------------------------------------------------------------------


async def test_an_alert_is_its_text_first_then_the_best_frame_and_the_album_as_replies(
    tmp_path: Path,
) -> None:
    b = await bot(tmp_path)
    event_id = await event(b, "20260115-101500", people=known_people("unknown"))
    sent = await alert(b, event_id)
    assert len(sent) == 1, "recipients: first: the trial goes to the owner only"
    order = [m for m in b.fake.methods() if m in ("sendMessage", "sendPhoto", "sendMediaGroup")]
    assert order == ["sendMessage", "sendPhoto", "sendMediaGroup"], "the text never waits"
    message = b.fake.last("sendMessage").params
    assert message["chat_id"] == OWNER and message["message_thread_id"] == ALERTS
    assert "reply_parameters" not in message
    text = message["text"]
    assert "Тревога: критично" in text and "Дверь" in text and "незнакомец" in text
    assert "Человек открывает щиток." in text and "—" not in text
    buttons = [b["text"] for row in message["reply_markup"]["inline_keyboard"] for b in row]
    assert buttons == ["👍 верно", "👎 ложная", "Не знаю"], "no face of a known person to name"
    photo = b.fake.last("sendPhoto")
    assert json.loads(photo.params["reply_parameters"])["message_id"] == 101
    assert photo.params["message_thread_id"] == str(ALERTS) and "file" in photo.files
    album = b.fake.last("sendMediaGroup")
    assert album.params["chat_id"] == str(OWNER)
    assert album.params["message_thread_id"] == str(ALERTS)
    assert json.loads(album.params["reply_parameters"])["message_id"] == 101
    media = json.loads(album.params["media"])
    assert [m["type"] for m in media] == ["photo"] * 2 + ["video"]
    assert set(album.files) == {"frame0", "frame1", "clip"}
    row = sent[0]
    assert row.data["media"] == "sent" and row.data["clip_attached"] is True
    assert row.data["frame"] == 102 and row.data["album"] == [103, 104, 105]
    timing = row.data["timing"]
    assert timing["text"] <= timing["frame"] <= timing["media"]

    assert await alert(b, event_id) == [], "one alert per event and chat"
    assert len(b.fake.all("sendMessage")) == 1
    await b.db.close()


async def test_every_recipient_gets_the_text_before_anyone_gets_media(tmp_path: Path) -> None:
    """17.11: the second recipient's alert waited for the first one's album and clip."""
    b = await bot(tmp_path, recipients="all")
    event_id = await event(b, "20260115-101500")
    sent = await alert(b, event_id)
    assert len(sent) == 2
    calls = [c for c in b.fake.calls if c.method in ("sendMessage", "sendPhoto", "sendMediaGroup")]
    first_media = next(i for i, c in enumerate(calls) if c.method != "sendMessage")
    texts = [c for c in calls if c.method == "sendMessage"]
    assert len(texts) == 2 and all(i < first_media for i, c in enumerate(calls)
                                   if c.method == "sendMessage")  # fmt: skip
    await b.db.close()


async def test_an_interrupted_recording_is_said_and_the_album_shows_what_the_judge_saw(
    tmp_path: Path,
) -> None:
    b = await bot(tmp_path)
    judged = []
    for n in (1, 2):
        frame = tmp_path / "judge-frames" / f"{n}.jpg"
        frame.parent.mkdir(parents=True, exist_ok=True)
        frame.write_bytes(b"\xff\xd8\xff\xd9")
        judged.append({"path": str(frame), "t": 70.0 + n})
    stream = {"stalls": 38, "longest_stall_s": 13.2, "missing_s": 83.7, "interrupted": True}
    event_id = await event(
        b, "20260115-101500", extra={"frames": judged, "stream": stream}
    )
    await alert(b, event_id)
    assert b.fake.last("sendPhoto").files["file"][0] == "1.jpg"
    media = json.loads(b.fake.last("sendMediaGroup").params["media"])
    assert [m["type"] for m in media] == ["photo", "video"]
    assert "📼 Запись прерывалась: 38 пауз, до 13 с" in b.fake.last("sendMessage").params["text"]
    await b.db.close()


async def test_the_album_shows_the_subject_not_an_empty_street(tmp_path: Path) -> None:
    """Frames the worker chose because they show the subject (`intent.album`) come first
    and alone; without them, the judged frames from the first one with a person."""
    b = await bot(tmp_path, frames=3)
    judged, album = [], []
    for n in (1, 2, 3, 4):
        frame = tmp_path / "judge-frames" / f"{n}.jpg"
        frame.parent.mkdir(parents=True, exist_ok=True)
        frame.write_bytes(b"\xff\xd8\xff\xd9")
        judged.append({"path": str(frame), "t": float(n)})
    face = tmp_path / "judge-frames" / "face-1.jpg"
    face.write_bytes(b"\xff\xd8\xff\xd9")
    album = [{"path": str(face), "t": 2.6, "why": "face"},
             {"path": judged[2]["path"], "t": 3.0, "why": "subject"}]  # fmt: skip
    with_album = await event(b, "20260115-101500", extra={"frames": judged, "album": album})
    await alert(b, with_album)
    assert b.fake.last("sendPhoto").files["file"][0] == "face-1.jpg", "the best frame first"
    media = json.loads(b.fake.last("sendMediaGroup").params["media"])
    assert [m["type"] for m in media] == ["photo", "video"]
    sent = b.fake.last("sendMediaGroup").files
    assert sent["frame0"][0] == "3.jpg"

    b.clock[0] += 600  # past `collapse`: a new alert, not a line in the first one
    older = await event(b, "20260115-101600", extra={"frames": judged})
    row = await b.db.get_event(older)
    assert row is not None and row.intent is not None
    row.intent["verdicts"][0]["person_at_s"] = 3.0
    await b.db.update_event(older, intent=row.intent)
    await alert(b, older)
    assert b.fake.last("sendPhoto").files["file"][0] == "3.jpg"
    sent = b.fake.last("sendMediaGroup").files
    assert [sent[k][0] for k in sorted(sent) if k.startswith("frame")] == ["4.jpg"]
    await b.db.close()


async def test_an_early_alert_is_completed_by_the_whole_clip_in_the_same_message(
    tmp_path: Path,
) -> None:
    b = await bot(tmp_path)
    early_clip = b.root / ".early" / "20260115-101500.mp4"
    early_clip.parent.mkdir(parents=True, exist_ok=True)
    early_clip.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    event_id = await event(b, "20260115-101500", level="elevated", scenario="person_at_house",
                           extra={"phase": "early"})  # fmt: skip
    await b.db.update_event(event_id, clip_path=str(early_clip))
    (sent,) = await alert(b, event_id)
    text = b.fake.last("sendMessage").params["text"]
    assert "По первым секундам записи" in text
    assert sent.data["early"] is True and sent.data["clip_attached"] is False
    album = sent.data["album"]

    # the whole clip: the same scenario, now judged on all of it
    row = await b.db.get_event(event_id)
    assert row is not None
    whole = Path(str(row.source_ref))
    intent = dict(row.intent or {}, phase="final")
    await b.db.update_event(event_id, clip_path=str(whole), intent=intent)
    row = await b.db.get_event(event_id)
    assert row is not None
    await b.service.refresh_event(row)
    media = b.fake.last("editMessageMedia")
    assert int(media.params["message_id"]) == album[-1] and "file" in media.files
    assert "Уточнено по всему ролику." in b.fake.last("editMessageText").params["text"]
    assert len(b.fake.all("sendMessage")) == 1, "the same level: no second buzz"

    # the level went up: one more message, answering the alert
    verdicts = [dict(intent["verdicts"][0], label="touching_equipment", level="critical")]
    await b.db.update_event(event_id, intent=dict(intent, verdicts=verdicts))
    row = await b.db.get_event(event_id)
    assert row is not None
    await b.service.refresh_event(row)
    up = b.fake.last("sendMessage").params
    assert "Уровень выше: критично" in up["text"]
    assert up["reply_parameters"]["message_id"] == sent.message_id
    assert len(b.fake.all("editMessageMedia")) == 1, "the clip is attached once"

    # and down: the whole clip says it was not an alert
    await b.db.update_event(event_id, state="dismissed")
    row = await b.db.get_event(event_id)
    assert row is not None
    await b.service.refresh_event(row)
    assert "это не тревога" in b.fake.last("editMessageText").params["text"]
    await b.db.close()


def test_where_the_time_went() -> None:
    from vidette.telegram.text import latency_text

    t = 1_794_694_000.0
    timeline = {
        "requested": t, "first_frame": t + 2, "person_seen": t + 79, "closed": t + 129,
        "picked_up": t + 135, "judge_started": t + 253, "judge_finished": t + 317,
    }  # fmt: skip
    text = latency_text(timeline, sent_at=t + 320)
    assert text is not None
    assert "→ человек в кадре: +1:17" in text and "→ судья начал: +1:58" in text
    assert "→ тревога ушла: +3 с" in text
    assert "От человека в кадре до тревоги: 4:01" in text
    early = {**timeline, "early": t + 11, "early_picked_up": t + 12,
             "early_judge_started": t + 12, "early_judge_finished": t + 20}  # fmt: skip
    text = latency_text(early, sent_at=t + 22)
    assert text is not None and "ролик готов" not in text  # later than the alert
    assert "→ судья ответил: +8 с" in text and "→ тревога ушла: +2 с" in text
    assert latency_text({"requested": t}, sent_at=None) is None


async def test_a_series_on_one_camera_edits_the_first_alert(tmp_path: Path) -> None:
    b = await bot(tmp_path)
    first = await event(b, "20260115-101500")
    await alert(b, first)
    second = await event(b, "20261114-170301", level="elevated", scenario="person_at_house")
    collapsed = await alert(b, second)
    assert len(b.fake.all("sendMessage")) == 1, "a lower level within the window: no new push"
    assert collapsed and collapsed[0].data["extra"]
    edit = b.fake.last("editMessageText").params
    assert "Ещё на этой камере: 1" in edit["text"]

    b.clock[0] += 3600  # outside the collapse window
    third = await event(b, "20261114-180000")
    await alert(b, third)
    assert len(b.fake.all("sendMessage")) == 2
    await b.db.close()


async def test_known_people_and_quiet_hours_go_to_the_digest(tmp_path: Path) -> None:
    b = await bot(tmp_path)
    sam = await event(b, "20260115-101500", level="elevated", scenario="person_at_house",
                      people=known_people("known"), persons=1)  # fmt: skip
    assert await alert(b, sam) == []
    await b.service._command(command("/quiet")["message"])
    quiet = b.fake.last("sendMessage").params["text"]
    assert "Тишина до" in quiet and "Критичные" in quiet
    stranger = await event(b, "20261114-170301", level="elevated", scenario="person_at_house",
                           people=known_people("unknown"))  # fmt: skip
    assert await alert(b, stranger) == [], "quiet hours hold an elevated alert"
    critical = await event(b, "20261114-170402", people=known_people("known"))
    assert len(await alert(b, critical)) == 1, "critical wakes even in quiet hours, even known"
    await b.db.close()


async def test_a_test_alert_says_where_it_would_have_gone(tmp_path: Path) -> None:
    b = await bot(tmp_path)
    dismissed = await event(b, "20260115-101500", state="dismissed", scenario="passer_by",
                            level="none")  # fmt: skip
    row = await b.db.get_event(dismissed)
    assert row is not None
    sent = await b.service.notify_event(row, test=True)
    assert len(sent) == 1 and sent[0].kind == "test"
    text = b.fake.last("sendMessage").params["text"]
    assert "Проверка канала" in text and "не считает это тревогой" in text
    await b.service.handle_update(callback(b, sent[0], "no"))
    labels = yaml.safe_load((b.root / "labels.yaml").read_text())
    assert labels["entries"][0]["scenario"] == "unlabelled", "missed alert: truth unknown"
    real = await event(b, "20261114-170301")
    assert len(await alert(b, real)) == 1, "a real alert never hides inside a test one"
    assert len(b.fake.all("sendMessage")) == 2
    assert len(await b.service.notify_event(row, test=True)) == 1, "a test may be repeated"
    unjudged = await event(b, "20261114-170402", state="observed", scenario=None, level=None,
                           summary="no verdict: server: HTTP 400: exceeds the available "
                           "context size (2048 tokens)")  # fmt: skip
    row = await b.db.get_event(unjudged)
    assert row is not None
    await b.service.notify_event(row, test=True)
    text = b.fake.last("sendMessage").params["text"]
    assert "Вердикта нет: кадры не помещаются в модель судьи" in text and "HTTP" not in text
    await b.db.close()


# --- buttons -----------------------------------------------------------------------------


async def test_buttons_become_reviewed_labels_and_edit_the_alert(tmp_path: Path) -> None:
    b = await bot(tmp_path)
    event_id = await event(b, "20260115-101500")
    [row] = await alert(b, event_id)

    def keyboard() -> list[list[str]]:
        markup = b.fake.last("editMessageText").params["reply_markup"]["inline_keyboard"]
        return [[button["text"] for button in line] for line in markup]

    def entry() -> dict[str, Any]:
        [found] = yaml.safe_load((b.root / "labels.yaml").read_text())["entries"]
        return dict(found)

    await b.service.handle_update(callback(b, row, "ok"))
    labels = yaml.safe_load((b.root / "labels.yaml").read_text())
    assert "touching_equipment" in labels["scenarios"] and "other" in labels["scenarios"]
    assert entry()["id"] == "20260115-101500" and entry()["status"] == "reviewed"
    assert entry()["scenario"] == "touching_equipment" and entry()["number"] == 1
    assert entry()["draft"]["by"].startswith("vlm-local fake-vlm (live)")
    assert keyboard() == [["👍 верно"]], "one button says what was chosen"
    assert b.fake.last("answerCallbackQuery").params["text"] == "Отмечено: 👍 верно"
    feedback = await b.db.get_event(event_id)
    assert feedback is not None and feedback.feedback == "up"

    await b.service.handle_update(callback(b, row, "undo", update_id=2))
    assert keyboard()[0] == ["👍 верно", "👎 ложная"], "tapping it brings the buttons back"
    assert entry()["status"] == "draft" and "reason" not in entry(), "and takes the label back"
    undone = await b.db.get_event(event_id)
    assert undone is not None and undone.feedback is None
    assert "review" not in (undone.intent or {})

    await b.service.handle_update(callback(b, row, "no", update_id=3))
    assert entry()["scenario"] == "other" and entry()["status"] == "reviewed"
    reasons = [text for line in keyboard() for text in line]
    assert "Прошёл мимо" in reasons and reasons[-1] == "Без причины"
    assert reasons[-2] == "Другое: напишу"
    await b.service.handle_update(callback(b, row, "r:passing", update_id=4))
    assert keyboard() == [["👎 прошёл мимо"]]
    assert entry()["scenario"] == "passer_by" and entry()["reason"] == "passing"
    reviewed = await b.db.get_event(event_id)
    assert reviewed is not None and reviewed.feedback == "down"
    assert (reviewed.intent or {})["review"]["reason"] == "passing"

    await b.service.handle_update(callback(b, row, "r:unimportant", update_id=5))
    assert entry()["scenario"] == "touching_equipment", "right, just not worth an alert"
    assert entry()["reason"] == "unimportant"

    await b.service.handle_update(callback(b, row, "r:other", update_id=6))
    question = b.fake.last("sendMessage")
    assert question.params["reply_markup"]["force_reply"] is True
    [(_chat, asked)] = list(b.service._prompts)
    reply = {
        "update_id": 7,
        "message": {
            "message_id": 999, "date": int(b.clock[0]), "chat": {"id": OWNER},
            "text": "соседи   паркуются", "reply_to_message": {"message_id": asked},
        },
    }  # fmt: skip
    await b.service.handle_update(reply)
    assert entry()["reason"] == "other" and entry()["notes"] == "соседи паркуются"
    assert keyboard() == [["👎 другое"]]

    await b.service.handle_update(callback(b, row, "undo", update_id=8))
    await b.service.handle_update(callback(b, row, "unk", update_id=9))
    assert entry()["scenario"] == "unlabelled" and entry()["status"] == "reviewed"
    assert "notes" not in entry(), "the earlier answer was taken back exactly"
    assert keyboard() == [["🤷 не знаю"]]
    await b.db.close()


async def test_presses_from_other_chats_and_stale_buttons_do_nothing(tmp_path: Path) -> None:
    b = await bot(tmp_path)
    [row] = await alert(b, await event(b, "20260115-101500"))
    before = len(b.fake.calls)
    await b.service.handle_update(callback(b, row, "ok", chat=STRANGER_CHAT))
    assert len(b.fake.calls) == before, "a stranger gets no answer at all"
    assert not (b.root / "labels.yaml").exists()
    stale = callback(b, row, "ok")
    stale["callback_query"]["message"]["message_id"] = 1
    await b.service.handle_update(stale)
    assert b.fake.last("answerCallbackQuery").params["text"] == "Эта тревога уже недоступна"
    await b.db.close()


async def test_this_is_one_of_us_confirms_the_face(tmp_path: Path) -> None:
    store = enrolled(tmp_path, sam=4)
    media = tmp_path / "media"
    report = build_report(
        [FakeFaceEngine().faces(photo("face:sam/new"))], store.gallery(), engine="fake",
        threshold=0.4, crops=crop_writer(media, "faces/ev"),
    )  # fmt: skip
    assert report.known, "Sam's face, recognized"
    b = await bot(tmp_path, store=store)
    event_id = await event(b, "20260115-101500", level="elevated", scenario="person_at_house",
                           people=report.model_dump(mode="json"), persons=2)  # fmt: skip
    [row] = await alert(b, event_id)
    buttons = b.fake.last("sendMessage").params["reply_markup"]["inline_keyboard"]
    assert buttons[1][0]["text"] == "Кто это?"

    await b.service.handle_update(callback(b, row, "own"))  # one face: straight to its menu
    picker = b.fake.last("editMessageText").params["reply_markup"]["inline_keyboard"]
    assert picker[0][0]["text"] == "Свой: Sam" and picker[-1][0]["text"] == "← Назад"
    await b.service.handle_update(callback(b, row, "p:1:0", update_id=2))
    markup = b.fake.last("editMessageText").params["reply_markup"]["inline_keyboard"]
    assert [button["text"] for button in markup[0]] == ["👍", "👎", "👤 свой: Sam"]
    assert store.view(store.require("sam")).confirmed == 1
    updated = await b.db.get_event(event_id)
    assert updated is not None and updated.intent is not None
    assert updated.intent["people"]["seen"][0]["confirmed"] == "sam"
    labels = yaml.safe_load((b.root / "labels.yaml").read_text())
    assert labels["entries"][0]["scenario"] == "household_member"

    await b.service.handle_update(callback(b, row, "u:1", update_id=3))  # tap «👤 свой: Sam»
    markup = b.fake.last("editMessageText").params["reply_markup"]["inline_keyboard"]
    assert markup[0][0]["text"] == "👍 верно", "taken back: the full buttons again"
    assert store.view(store.require("sam")).confirmed == 0
    labels = yaml.safe_load((b.root / "labels.yaml").read_text())
    assert labels["entries"][0]["status"] == "draft"
    await b.db.close()


# --- commands and the digest -------------------------------------------------------------


async def test_commands_answer_allowed_chats_where_they_were_asked(tmp_path: Path) -> None:
    b = await bot(tmp_path)
    await b.service.handle_update(command("/live", chat=MEMBER, thread=5))
    live = b.fake.last("sendMessage").params
    assert live["chat_id"] == MEMBER and live["message_thread_id"] == 5
    assert "в приложении Vidette" in live["text"] and "http" not in live["text"]
    before = len(b.fake.calls)
    await b.service.handle_update(command("/live", chat=STRANGER_CHAT))
    await b.service.handle_update(command("/status", age=3600))
    assert len(b.fake.calls) == before, "strangers and old commands get nothing"
    await b.service.handle_update(command("/quiet@fake_vidette_bot off"))
    assert "Тишина выключена" in b.fake.last("sendMessage").params["text"]
    await b.db.close()


async def test_status_and_the_digest(tmp_path: Path) -> None:
    b = await bot(tmp_path)
    now = b.clock[0]
    old = now - 1800
    [first] = await alert(b, await event(b, "20261114-170000", started=old))
    await event(b, "20261114-170100", state="dismissed", scenario="passer_by", level="none",
                started=old + 60)  # fmt: skip
    await event(b, "20261114-170200", state="dismissed", scenario="passer_by", level="none",
                started=old + 120)  # fmt: skip
    await event(b, "20261114-170300", state="observed", scenario=None, level=None,
                started=old + 180, summary="no verdict: timeout: no answer")  # fmt: skip
    await event(b, "20261114-170400", level="elevated", scenario="person_at_house",
                people=known_people("known"), started=old + 240)  # fmt: skip
    b.clock[0] += 600  # past `collapse`: its own alert
    [second] = await alert(b, await event(b, "20260115-111500", started=now - 60))

    await b.service.handle_update(command("/status", thread=ALERTS, age=-600))
    status, digest = (c.params for c in b.fake.all("sendMessage")[-2:])
    assert "Дверь: 6 событий, тревог 3" in status["text"]
    assert "Последняя тревога" in status["text"] and "критично" in status["text"]
    text = digest["text"]
    assert digest["message_thread_id"] == ALERTS and "Сегодня к" in text
    assert "Тревог: 2. По вашим отметкам: без ответа 2." in text
    assert "2 тревоги ждут ответа" in text
    assert "Дверь: внимание, свой: Sam" in text, "confirmed but woke nobody: worth a look"
    assert "Всего за день 6 роликов: прохожие 2." in text
    assert "Не проверено судьёй: 1 (судья не успел)." in text
    assert "×" not in text, "plain words, no codes"

    await b.service.handle_update(callback(b, first, "no"))
    await b.service.handle_update(callback(b, first, "r:passing", update_id=2))
    await b.service.handle_update(callback(b, second, "ok", update_id=3))
    day = await b.service.day(OWNER)
    assert (day.alerts, day.wrong, day.right, day.unanswered) == (2, 1, 1, 0)
    assert day.reasons["прошёл мимо"] == 1

    sent = len(b.fake.all("sendMessage"))
    assert await b.service.evening_digest(OWNER) is True, "something worth a look"
    evening = b.fake.last("sendMessage").params
    assert evening["message_thread_id"] == DIGEST and evening["disable_notification"] is True
    assert "Итог дня" in evening["text"]
    assert "Ложные чаще всего: прошёл мимо (1)." in evening["text"]
    assert await b.service.evening_digest(OWNER) is False, "once a day, also after a restart"
    restarted = await bot(tmp_path)
    assert await restarted.service.evening_digest(OWNER) is False
    assert len(b.fake.all("sendMessage")) == sent + 1
    await restarted.db.close()
    await b.db.close()


async def test_an_evening_with_nothing_to_look_at_sends_nothing(tmp_path: Path) -> None:
    b = await bot(tmp_path)
    await event(b, "20261114-170100", state="dismissed", scenario="passer_by", level="none")
    [row] = await alert(b, await event(b, "20261114-170000"))
    await b.service.handle_update(callback(b, row, "ok"))
    sent = len(b.fake.all("sendMessage"))
    assert await b.service.evening_digest(OWNER) is False
    assert len(b.fake.all("sendMessage")) == sent
    await b.db.close()


# --- the API client and the poller -------------------------------------------------------


async def test_the_client_waits_when_telegram_asks_and_never_leaks_the_token() -> None:
    fake = FakeTelegram()
    waited: list[float] = []

    async def sleep(seconds: float) -> None:
        waited.append(seconds)

    api = TelegramApi(TOKEN, base_url="http://telegram.test", transport=fake.transport(),
                      sleep=sleep)  # fmt: skip
    fake.fail("sendMessage", 429, "Too Many Requests: retry after 3", retry_after=3)
    await api.send_message(OWNER, "hi")
    assert waited == [3.0] and len(fake.all("sendMessage")) == 2
    fake.fail("sendMessage", 400, "Bad Request: chat not found")
    with pytest.raises(TelegramError) as caught:
        await api.send_message(OWNER, "hi")
    assert "chat not found" in str(caught.value) and TOKEN not in str(caught.value)
    bad = TelegramApi("1:wrong-token-wrong-token-wrong", base_url="http://telegram.test",
                      transport=fake.transport(), sleep=sleep)  # fmt: skip
    with pytest.raises(TelegramError) as caught:
        await bad.get_me()
    assert caught.value.code == 401 and "wrong-token" not in str(caught.value)
    await api.aclose()
    await bad.aclose()


async def test_the_poller_handles_updates_and_remembers_the_offset(tmp_path: Path) -> None:
    b = await bot(tmp_path)
    b.fake.updates = [{**command("/help"), "update_id": 41}]
    task = asyncio.create_task(b.service._poll())
    for _ in range(100):
        if await b.db.get_meta("telegram.offset") == "42":
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await b.db.get_meta("telegram.offset") == "42"
    assert "setMyCommands" in b.fake.methods()
    assert "Я присылаю тревоги" in b.fake.last("sendMessage").params["text"]
    await b.db.close()


# --- when Telegram cannot be reached ------------------------------------------------------


class StaleTransports:
    """Transports for TelegramApi's clients: the first `stale` never connect (a pool that
    went bad while the machine itself reaches Telegram), the later ones reach the fake."""

    def __init__(self, fake: FakeTelegram, stale: int) -> None:
        self.fake, self.stale, self.made = fake, stale, 0

    def __call__(self) -> httpx.AsyncBaseTransport:
        self.made += 1
        if self.made <= self.stale:

            def refuse(request: httpx.Request) -> httpx.Response:
                raise httpx.ConnectError("connection refused", request=request)

            return httpx.MockTransport(refuse)
        return self.fake.transport()


async def until(check: Any, what: str) -> None:
    for _ in range(300):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"never happened: {what}")


def today_at(hour: int, minute: int = 0, second: int = 0) -> float:
    at = datetime.now().replace(hour=hour, minute=minute, second=second, microsecond=0)
    return at.timestamp()


class StopLoop(Exception):  # noqa: N818 (not an error: the end of a test)
    """Ends a scheduler loop in a test: it asked to sleep until tomorrow."""


async def test_errors_say_whether_telegram_may_have_got_the_message() -> None:
    fake = FakeTelegram()
    api = TelegramApi(TOKEN, base_url="http://telegram.test", transport=fake.transport(),
                      sleep=no_sleep)  # fmt: skip
    fake.offline = True
    with pytest.raises(TelegramError) as caught:
        await api.send_message(OWNER, "hi", once=True)
    assert caught.value.unsent and caught.value.transient and caught.value.code is None
    assert fake.refused == 4, "it never left: tried again, safely"
    assert TOKEN not in str(caught.value)

    fake.offline = False
    fake.lose_answer("sendMessage")
    with pytest.raises(TelegramError) as caught:
        await api.send_message(OWNER, "hi", once=True)
    assert not caught.value.unsent and not caught.value.transient
    assert len(fake.all("sendMessage")) == 1, "it may have arrived: never repeated"
    fake.lose_answer("sendMessage")
    await api.send_message(OWNER, "hi")
    assert len(fake.all("sendMessage")) == 3, "an ordinary message is tried again, as before"
    fake.lose_answer("sendMediaGroup")
    with pytest.raises(TelegramError):
        await api.send_media_group(OWNER, [{"type": "photo", "media": "attach://a"}] * 2,
                                   {"a": ("a.jpg", b"jpeg", "image/jpeg")})  # fmt: skip
    assert len(fake.all("sendMediaGroup")) == 1, "an upload that may have arrived: never twice"

    for _ in range(4):
        fake.fail("sendMessage", 502, "Bad Gateway")
    with pytest.raises(TelegramError) as caught:
        await api.send_message(OWNER, "hi")
    assert caught.value.transient, "Telegram answered with a server error: it can go later"
    fake.fail("sendMessage", 400, "Bad Request: chat not found")
    with pytest.raises(TelegramError) as caught:
        await api.send_message(OWNER, "hi")
    assert caught.value.unsent and not caught.value.transient, "will not fix itself"
    await api.aclose()


async def test_a_client_without_an_answer_for_minutes_is_replaced_and_it_is_said_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="vidette.telegram.api")
    fake = FakeTelegram()
    transports = StaleTransports(fake, stale=3)
    now = [1000.0]
    api = TelegramApi(TOKEN, base_url="http://telegram.test", transport_factory=transports,
                      sleep=no_sleep, clock=lambda: now[0], reconnect_after=180)  # fmt: skip
    with pytest.raises(TelegramError):
        await api.get_me()
    assert api.reconnects == 0, "a short outage keeps the client"
    for _ in range(2):  # still no answer: a new client every 3 minutes
        now[0] += 200
        with pytest.raises(TelegramError):
            await api.get_me()
    assert api.reconnects == 2
    now[0] += 200
    assert (await api.get_me())["username"] == "fake_vidette_bot", "the fourth client works"
    assert api.reconnects == 3 and transports.made == 4
    said = [r.getMessage() for r in caplog.records]
    assert sum("replaced by a new one" in line for line in said) == 1, "logged once"
    assert any("answers again" in line for line in said)
    assert TOKEN not in caplog.text

    now[0] += 3600  # answers all along: nothing to replace
    await api.get_me()
    assert api.reconnects == 3
    await api.aclose()


async def test_a_new_client_never_cuts_a_request_in_flight() -> None:
    fake = FakeTelegram()
    release = asyncio.Event()

    class Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await release.wait()
            return fake.handle(request)

    made: list[httpx.AsyncBaseTransport] = []

    def transport() -> httpx.AsyncBaseTransport:
        made.append(Slow() if not made else fake.transport())
        return made[-1]

    api = TelegramApi(TOKEN, base_url="http://telegram.test", transport_factory=transport,
                      sleep=no_sleep)  # fmt: skip
    poll = asyncio.create_task(api.get_updates(None, timeout=1))
    await asyncio.sleep(0.02)
    old = api._client
    await api.reconnect()
    assert not old.is_closed, "the long poll still runs on it"
    assert (await api.get_me())["is_bot"], "new requests take the new client"
    release.set()
    assert await poll == []
    assert old.is_closed, "closed when its last request was over"
    await api.aclose()


async def test_a_digest_goes_again_only_when_telegram_never_got_it(tmp_path: Path) -> None:
    b = await bot(tmp_path)
    b.clock[0] = today_at(21, 0, 1)
    await event(b, "20261114-170400", level="elevated", scenario="person_at_house",
                people=known_people("known"), started=b.clock[0] - 600)  # fmt: skip
    key = f"telegram.digest_day:{OWNER}"
    today = datetime.fromtimestamp(b.clock[0]).date().isoformat()
    b.fake.offline = True
    with pytest.raises(TelegramError) as caught:
        await b.service.evening_digest(OWNER)
    assert caught.value.transient
    assert await b.db.get_meta(key) == f"{today}:unsent", "Telegram never got it: owed"
    b.fake.offline = False
    assert await b.service.evening_digest(OWNER) is True
    assert await b.db.get_meta(key) == today
    assert await b.service.evening_digest(OWNER) is False, "once"
    assert len(b.fake.all("sendMessage")) == 1

    # the next evening the answer is lost after the digest went out: it may have arrived
    b.clock[0] += 86400
    await event(b, "20261115-170400", level="elevated", scenario="person_at_house",
                people=known_people("known"), started=b.clock[0] - 600)  # fmt: skip
    b.fake.lose_answer("sendMessage")
    with pytest.raises(TelegramError) as caught:
        await b.service.evening_digest(OWNER)
    assert not caught.value.transient
    tomorrow = datetime.fromtimestamp(b.clock[0]).date().isoformat()
    assert await b.db.get_meta(key) == tomorrow, "marked as sent: never twice"
    assert await b.service.evening_digest(OWNER) is False
    assert len(b.fake.all("sendMessage")) == 2, "it reached Telegram once and is not repeated"
    await b.db.close()


async def test_the_evening_digest_is_retried_until_it_lands(tmp_path: Path) -> None:
    b = await bot(tmp_path)
    start = today_at(21, 0, 1)
    b.clock[0] = start
    await event(b, "20261114-170400", level="elevated", scenario="person_at_house",
                people=known_people("known"), started=start - 600)  # fmt: skip
    b.fake.offline = True
    pauses: list[float] = []

    async def sleep(seconds: float) -> None:
        if seconds > 600:
            raise StopLoop  # longer than any pause between tries: until tomorrow
        pauses.append(seconds)
        b.clock[0] += seconds
        b.fake.offline = b.clock[0] < start + 400  # Telegram is back after ~7 minutes

    b.service._sleep = sleep
    with pytest.raises(StopLoop):
        await b.service._digests()
    assert pauses == [30, 60, 120, 240], "a growing pause between tries"
    [digest] = b.fake.all("sendMessage")
    assert "Итог дня" in digest.params["text"] and digest.params["message_thread_id"] == DIGEST
    await b.db.close()


async def test_a_digest_that_cannot_go_by_digest_until_is_not_sent_late(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    b = await bot(tmp_path)
    start = today_at(21, 0, 1)
    b.clock[0] = start
    await event(b, "20261114-170400", level="elevated", scenario="person_at_house",
                people=known_people("known"), started=start - 600)  # fmt: skip
    b.fake.offline = True
    pauses: list[float] = []

    async def sleep(seconds: float) -> None:
        if seconds > 600:
            raise StopLoop
        pauses.append(seconds)
        b.clock[0] += seconds

    b.service._sleep = sleep
    with pytest.raises(StopLoop):
        await b.service._digests()
    assert max(pauses) == 600, "the pause grows to ten minutes"
    assert b.clock[0] == today_at(23, 30), "the last try was at 23:30 sharp"
    assert not b.fake.all("sendMessage") and b.fake.refused > 0
    today = datetime.fromtimestamp(start).date().isoformat()
    assert await b.db.get_meta(f"telegram.digest_day:{OWNER}") == today, "given up for today"
    assert "did not reach Telegram by 23:30" in caplog.text

    # back at 23:40 (a restart, or Telegram again): not sent late any more
    b.fake.offline = False
    b.clock[0] = today_at(23, 40)
    with pytest.raises(StopLoop):
        await b.service._digests()
    assert not b.fake.all("sendMessage")
    await b.db.close()


def test_digest_until_is_checked(tmp_path: Path) -> None:
    tg = make_config(tmp_path, digest_at="23:45").notifications.telegram
    assert tg is not None and tg.digest_until_minutes == 24 * 60, "the default is not before it"
    tg = make_config(tmp_path, digest_until=None).notifications.telegram
    assert tg is not None and tg.digest_until_minutes == 24 * 60
    tg = make_config(tmp_path).notifications.telegram
    assert tg is not None and tg.digest_until_minutes == 23 * 60 + 30
    with pytest.raises(ValueError, match="digest_until must be later than digest_at"):
        make_config(tmp_path, digest_at="22:00", digest_until="21:30")
    with pytest.raises(ValueError, match="digest_until must be a local time"):
        make_config(tmp_path, digest_until="late")
    with pytest.raises(ValueError, match="late_alerts_for"):
        make_config(tmp_path, late_alerts_for="soon")


async def test_an_alert_telegram_could_not_take_goes_late_once(tmp_path: Path) -> None:
    b = await bot(tmp_path)
    b.clock[0] = today_at(12)
    event_id = await event(b, "20260115-101500", people=known_people("unknown"))
    b.fake.offline = True
    assert await alert(b, event_id) == []
    [waiting] = await b.db.pending_telegram_messages()
    assert waiting.message_id is None and waiting.data["pending"]["what"] == "send"
    assert b.service.status.failures == 1
    assert await alert(b, event_id) == [], "still one alert per event and chat"
    assert await b.service.resend_pending() is False, "still out of reach: it keeps waiting"
    assert len(await b.db.pending_telegram_messages()) == 1

    # an hour later Telegram answers again; the service was restarted meanwhile
    restarted = await bot(tmp_path)
    restarted.clock[0] = b.clock[0] + 3600
    assert await restarted.service.resend_pending() is True
    await restarted.service.media_settled()
    media = json.loads(restarted.fake.last("sendMediaGroup").params["media"])
    assert [m["type"] for m in media] == ["photo"] * 2 + ["video"]
    message = restarted.fake.last("sendMessage").params
    due = datetime.fromtimestamp(waiting.created_at).strftime("%H:%M:%S")
    assert f"С опозданием: в {due} Telegram был недоступен." in message["text"]
    assert "Тревога: критично" in message["text"] and message["message_thread_id"] == ALERTS
    assert "reply_parameters" not in message, "the text goes first, the media answer it"
    sent = await restarted.db.get_telegram_message(waiting.id)
    assert sent is not None and sent.message_id is not None and "pending" not in sent.data
    assert await restarted.service.resend_pending() is True
    assert len(restarted.fake.all("sendMessage")) == 1, "once"

    await restarted.service.handle_update(callback(restarted, sent, "ok"))
    assert "С опозданием" in restarted.fake.last("editMessageText").params["text"]
    await restarted.db.close()
    await b.db.close()


async def test_alerts_that_waited_too_long_are_not_sent_and_the_chat_hears_it(
    tmp_path: Path,
) -> None:
    b = await bot(tmp_path, late_alerts_for="30m")
    b.clock[0] = today_at(12)
    first = await event(b, "20260115-101500")
    b.fake.offline = True
    await alert(b, first)
    b.clock[0] += 600  # past `collapse`
    second = await event(b, "20261114-170301")
    await alert(b, second)
    assert len(await b.db.pending_telegram_messages()) == 2
    b.clock[0] += 3600
    assert await b.service.resend_pending() is False, "the line about them waits too"
    assert await b.db.pending_telegram_messages() == []
    b.fake.offline = False
    assert await b.service.resend_pending() is True
    [note] = b.fake.all("sendMessage")
    assert "2 тревоги с 12:00 до 12:10 не отправлены" in note.params["text"], "when they were due"
    assert note.params["message_thread_id"] == ALERTS and not b.fake.all("sendMediaGroup")
    day = await b.service.day(OWNER)
    assert len(day.notable) == 2 and day.alerts == 0, "the digest lists them"
    await b.db.close()


async def test_late_alerts_and_edits_follow_the_usual_rules(tmp_path: Path) -> None:
    b = await bot(tmp_path)
    b.clock[0] = today_at(12)
    # the text did not go out: nothing else did either; later the text, then its media
    first = await event(b, "20260115-101500")
    for _ in range(4):
        b.fake.fail("sendMessage", 502, "Bad Gateway")
    assert await alert(b, first) == []
    [waiting] = await b.db.pending_telegram_messages()
    assert "album" not in waiting.data and not b.fake.all("sendMediaGroup")
    # a lower level on the same camera, due within `collapse`, while Telegram is away
    b.fake.offline = True
    b.clock[0] += 60
    second = await event(b, "20261114-170301", level="elevated", scenario="person_at_house")
    assert await alert(b, second) == []
    # an early alert the whole clip then called ordinary: not sent at all
    b.clock[0] += 900
    third = await event(b, "20261114-171800", extra={"phase": "early"})
    assert await alert(b, third) == []
    await b.db.update_event(third, state="dismissed")

    b.fake.offline = False
    b.clock[0] += 600
    assert await b.service.resend_pending() is True
    await b.service.media_settled()
    assert len(b.fake.all("sendMediaGroup")) == 1, "the album is sent once"
    assert [c for c in b.fake.all("sendMessage") if "С опозданием" in c.params["text"]]
    album = b.fake.last("sendMediaGroup").params
    went = await b.db.get_telegram_message(waiting.id)
    assert went is not None and went.message_id is not None
    assert json.loads(album["reply_parameters"])["message_id"] == went.message_id
    edit = b.fake.last("editMessageText").params["text"]
    assert "Ещё на этой камере: 1\n＋ 12:00:00" in edit, "folded in, as it would have been"
    assert len(b.fake.all("sendMessage")) == 5, "four refused tries and one late alert"
    alerted = await b.db.telegram_alerted_events(OWNER, since=0)
    assert alerted == {first, second}, "the ordinary one went nowhere"
    await b.db.close()


async def test_edits_of_alerts_wait_for_telegram_too(tmp_path: Path) -> None:
    b = await bot(tmp_path)
    b.clock[0] = today_at(12)
    early_clip = b.root / ".early" / "20260115-101500.mp4"
    early_clip.parent.mkdir(parents=True, exist_ok=True)
    early_clip.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    event_id = await event(b, "20260115-101500", extra={"phase": "early"})
    await b.db.update_event(event_id, clip_path=str(early_clip))
    (sent,) = await alert(b, event_id)

    b.fake.offline = True
    # another alert on the camera folds into this one: its edit waits
    other = await event(b, "20261114-170301", level="elevated", scenario="person_at_house")
    await alert(b, other)
    # and the whole clip, judged meanwhile, cannot reach the alert either
    row = await b.db.get_event(event_id)
    assert row is not None
    intent = dict(row.intent or {}, phase="final")
    await b.db.update_event(event_id, clip_path=str(row.source_ref), intent=intent)
    row = await b.db.get_event(event_id)
    assert row is not None
    await b.service.refresh_event(row)
    [waiting] = await b.db.pending_telegram_messages()
    assert waiting.id == sent.id and waiting.data["pending"]["what"] == "refresh"

    b.fake.offline = False
    assert await b.service.resend_pending() is True
    text = b.fake.last("editMessageText").params["text"]
    assert "Уточнено по всему ролику." in text and "Ещё на этой камере: 1" in text
    assert len(b.fake.all("editMessageMedia")) == 1, "the whole clip is attached"
    assert len(b.fake.all("sendMessage")) == 1, "no second buzz"
    await b.db.close()


async def test_the_queue_goes_out_as_soon_as_telegram_answers_again(tmp_path: Path) -> None:
    b = await bot(tmp_path)
    forever = asyncio.Event()

    async def long_pause(_seconds: float) -> None:
        await forever.wait()

    b.service._sleep = long_pause  # the pause between tries outlasts the test
    b.fake.offline = True
    await alert(b, await event(b, "20260115-101500"))
    b.service._online.clear()  # what the poller does when it cannot reach Telegram
    worker = asyncio.create_task(b.service._resend())
    await asyncio.sleep(0.05)
    assert not b.fake.all("sendMessage")
    b.fake.offline = False
    b.service._online.set()  # the poller heard Telegram again
    await until(lambda: bool(b.fake.all("sendMessage")), "the late alert")
    assert "С опозданием" in b.fake.last("sendMessage").params["text"]
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    await b.db.close()


async def test_the_poller_says_when_telegram_is_out_of_reach_and_back(tmp_path: Path) -> None:
    b = await bot(tmp_path)

    async def tick(_seconds: float) -> None:
        await asyncio.sleep(0)

    b.service._sleep = tick
    b.fake.offline = True
    task = asyncio.create_task(b.service._poll())
    await until(lambda: not b.service._online.is_set(), "offline")
    b.fake.offline = False
    await until(lambda: b.service._online.is_set(), "online again")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await b.db.close()


# --- end to end: `vidette telegram try` --------------------------------------------------


def test_try_judges_a_real_clip_folder_clip_and_sends_a_test_alert(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeTelegram()
    vlm = FakeVlmApi(
        answer=lambda _t, _i, _b: {"scenario": "touching_equipment", "persons": 1,
                                   "description": "Человек у щитка.", "confidence": 0.8}
    )  # fmt: skip
    dataset = tmp_path / "front-door"
    clip = collected_clip(dataset, "20260115-101500")
    with fake.serve() as telegram_url, vlm.serve() as vlm_url:
        config = make_config(tmp_path, api_url=telegram_url)
        data = config.model_dump(mode="json")
        data["notifications"]["telegram"]["token"] = "${VIDETTE_TELEGRAM_BOT_TOKEN}"
        data["understanding"]["vlm"]["local"]["base_url"] = vlm_url
        path = tmp_path / "vidette.yaml"
        path.write_text(yaml.safe_dump(data, allow_unicode=True))
        env = tmp_path / "telegram.env"
        env.write_text(f"VIDETTE_TELEGRAM_BOT_TOKEN={TOKEN}\n")
        env.chmod(0o600)
        os.environ.pop("VIDETTE_TELEGRAM_BOT_TOKEN", None)
        try:
            code = main(["telegram", "--config", str(path), "--env-file", str(env), "try",
                         str(clip)])  # fmt: skip
        finally:
            os.environ.pop("VIDETTE_TELEGRAM_BOT_TOKEN", None)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "verdict  confirmed" in out and TOKEN not in out
    text = fake.last("sendMessage").params["text"]
    assert "Проверка канала" in text and "Человек у щитка." in text
    assert "Тревога: критично" in text


def test_check_prints_counts_not_ids(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeTelegram()
    with fake.serve() as url:
        config = make_config(tmp_path, api_url=url)
        data = config.model_dump(mode="json")
        data["notifications"]["telegram"]["token"] = TOKEN
        path = tmp_path / "vidette.yaml"
        path.write_text(yaml.safe_dump(data, allow_unicode=True))
        assert main(["telegram", "--config", str(path), "check"]) == 0
    out = capsys.readouterr().out
    assert "@fake_vidette_bot" in out and "chat 1: gets alerts" in out
    assert str(OWNER) not in out and TOKEN not in out


def test_verdict_type_carries_level_and_persons() -> None:
    verdict = Verdict(alert=True, label="x", level="critical", persons=2)
    assert verdict.model_dump()["level"] == "critical"
    assert EventState.confirmed.value == "confirmed"
