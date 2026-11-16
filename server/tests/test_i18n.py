"""The bot's language (`notifications.telegram.language`): one catalog per language,
English the reference, every phrase from the catalog, free text from models in the same
language through the prompt."""

from __future__ import annotations

import re
import string
from datetime import datetime
from pathlib import Path

import pytest
from test_telegram import alert, bot, command, event, known_people

from vidette.assistant.agent import SYSTEM, Assistant
from vidette.assistant.tools import Tools
from vidette.core.config import VidetteConfig
from vidette.telegram import i18n
from vidette.telegram import text as say
from vidette.telegram.i18n import CATALOGS, EN, RU, n, ordinal, t
from vidette.vlm.budget import MemoryLedger

SERVER = Path(__file__).resolve().parents[1] / "vidette"
CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def fields(template: str) -> set[str]:
    return {name for _text, name, _spec, _conv in string.Formatter().parse(template) if name}


def test_every_language_has_every_phrase_with_the_same_placeholders() -> None:
    for code, catalog in CATALOGS.items():
        assert set(catalog) == set(EN), f"{code}: {set(EN) ^ set(catalog)}"
        for key, template in catalog.items():
            assert fields(template) == fields(EN[key]), f"{code} {key}"
    assert not any(CYRILLIC.search(v) for v in EN.values()), "English is English"
    for key in (k for k in EN if k.startswith("n.")):
        assert len(EN[key].split("|")) == 2 and len(RU[key].split("|")) == 3, key
    assert len(EN["months"].split("|")) == len(RU["months"].split("|")) == 12


def test_user_facing_code_says_nothing_outside_the_catalog() -> None:
    """A Cyrillic letter in the bot's or the assistant's code is a phrase that escaped the
    catalog; the only exceptions are the Russian words the bot understands as input."""
    allowed = {
        "assistant/agent.py": re.compile(r'^\s*r"|re\.compile\(r"'),  # question patterns
        "telegram/service.py": re.compile(r"QUIET_OFF_WORDS"),  # "/quiet выкл"
    }
    for relative in ("telegram/service.py", "telegram/text.py", "telegram/reasons.py",
                     "telegram/routing.py", "assistant/agent.py", "assistant/tools.py",
                     "linked.py", "runtime.py", "deep/worker.py"):  # fmt: skip
        for number, line in enumerate((SERVER / relative).read_text().splitlines(), start=1):
            if CYRILLIC.search(line):
                rule = allowed.get(relative)
                assert rule is not None and rule.search(line), f"{relative}:{number}: {line}"


def test_counts_and_ordinals_follow_the_language() -> None:
    i18n.use("en")
    assert [n("n.clips", k) for k in (1, 2, 5)] == ["1 clip", "2 clips", "5 clips"]
    assert [ordinal(k) for k in (1, 2, 3, 4, 11, 12, 21, 112)] == [
        "1st", "2nd", "3rd", "4th", "11th", "12th", "21st", "112th",
    ]  # fmt: skip
    i18n.use("ru")
    assert [n("n.clips", k) for k in (1, 3, 5, 11, 21, 22)] == [
        "1 ролик", "3 ролика", "5 роликов", "11 роликов", "21 ролик", "22 ролика",
    ]  # fmt: skip
    assert ordinal(3) == "3-й"
    i18n.use("xx")
    assert i18n.language() == "en", "an unknown language falls back to English"


def test_english_texts_read_as_english() -> None:
    i18n.use("en")
    now = datetime(2026, 11, 14, 21, 0).timestamp()
    facts = say.AlertFacts(
        camera="Porch", started_at=now - 60, level_rank=0, level_title="critical",
        description="A person tries the gate.", who=say.who_line(None, 0, names=False),
    )  # fmt: skip
    text = say.alert_text(facts, now=now)
    assert "Alert: critical" in text and "no people in view" in text
    day = say.DayDigest(since=now - 3600, events=3)
    day.ordinary["passers-by"] += 3
    digest = say.day_digest_text(day, now=now, evening=True)
    assert "The day, November 14" in digest and "No alerts." in digest
    assert "All day: 3 clips: passers-by 3." in digest
    assert say.answer_button({"answer": "wrong", "reason": "passing"}) == "👎 walked past"
    assert "/quiet off" in say.help_text() and not CYRILLIC.search(say.help_text())
    assert say.quiet_text(None, now=now).startswith("🔔 Quiet is off")


async def test_an_english_bot_sends_english_alerts_and_buttons(tmp_path: Path) -> None:
    b = await bot(tmp_path, language="en", camera_names={"front_door": "Front door"})
    assert i18n.language() == "en"
    event_id = await event(b, "20260115-101500", people=known_people("unknown"))
    await alert(b, event_id)
    message = b.fake.last("sendMessage").params
    assert "Alert: критично" in message["text"], "level titles are the config's own words"
    assert "stranger" in message["text"] and "Front door" in message["text"]
    buttons = [x["text"] for row in message["reply_markup"]["inline_keyboard"] for x in row]
    assert buttons == ["👍 right", "👎 false alarm", "Not sure"]
    await b.service.handle_update(command("/help"))
    assert b.fake.last("sendMessage").params["text"].startswith("I send alerts")
    await b.db.close()


async def test_a_russian_bot_keeps_speaking_russian(tmp_path: Path) -> None:
    i18n.use("en")
    b = await bot(tmp_path)  # language: ru in the test config
    event_id = await event(b, "20260115-101500", people=known_people("unknown"))
    await alert(b, event_id)
    message = b.fake.last("sendMessage").params
    buttons = [x["text"] for row in message["reply_markup"]["inline_keyboard"] for x in row]
    assert buttons == ["👍 верно", "👎 ложная", "Не знаю"]
    await b.db.close()


@pytest.mark.parametrize(
    ("language", "vlm_language", "expected"),
    [("ru", None, "Russian"), ("en", None, None), ("ru", "German", "German")],
)
def test_models_write_in_the_bots_language_unless_told_otherwise(
    language: str, vlm_language: str | None, expected: str | None
) -> None:
    config = VidetteConfig.model_validate(
        {
            "understanding": {"vlm": {"language": vlm_language}},
            "notifications": {
                "telegram": {
                    "token": "123456789:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKKllll",
                    "allowed_chats": "1",
                    "language": language,
                }
            },
        }
    )
    assert config.understanding.vlm.language == expected


async def test_the_assistant_is_told_to_answer_in_the_bots_language(tmp_path: Path) -> None:
    from test_assistant import fake_model, setup

    db, cfg, _event_id = await setup(tmp_path)
    seen: list[dict[str, object]] = []
    http = fake_model([{"content": "Ничего."}], seen)
    i18n.use("ru")
    assistant = Assistant(cfg, Tools(cfg, db), ledger=MemoryLedger(), http=http)
    await assistant.answer("что было на улице после 12?")
    system = str(seen[0]["messages"][0]["content"])  # type: ignore[index]
    assert "Answer in Russian" in system and "{language}" in SYSTEM
    assert t("assistant.off").startswith("Свободные")
    await db.close()
