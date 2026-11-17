"""The assistant never sends a tool transcript, takes "it was me, a test" as feedback, and
answers "what about the other camera" from the alert's incident (17.11)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from vidette.assistant.agent import NUDGE, Assistant, clean_answer, echoed_call, staged_by_us
from vidette.vlm.budget import MemoryLedger


def test_a_tool_transcript_is_never_an_answer() -> None:
    echo = '[tool_result]\n{"id": "01a1", "cam": "Улица"}\n[/tool_result]'
    assert clean_answer(echo) == ""
    assert clean_answer('{"id": "01a1", "cam": "x"}') == ""
    assert clean_answer("<tool_response>{}</tool_response> Это был сосед.") == "Это был сосед."
    assert clean_answer("```json\n{\"a\": 1}\n```") == ""
    assert clean_answer("Всё спокойно.") == "Всё спокойно."
    assert echoed_call('<tool_call>{"name": "event_detail", "arguments": {"event_id": "current"}}'
                       "</tool_call>") == ("event_detail", {"event_id": "current"})  # fmt: skip
    assert staged_by_us("верно, это был я, специально скрыл лицо")
    assert staged_by_us("it was me, a test")
    assert not staged_by_us("это был я"), "without a test word it may be a false alarm"


async def test_a_model_that_echoes_a_tool_result_is_asked_again_then_answered_plainly(
    tmp_path: Path,
) -> None:
    from test_assistant import fake_model, model_config, setup

    db, _cfg, event_id = await setup(tmp_path)
    cfg = model_config(tmp_path)
    echo = {"role": "assistant", "content": '[tool_result]\n{"id": "x"}\n[/tool_result]'}
    seen: list[dict[str, Any]] = []
    http = fake_model([echo, {"role": "assistant", "content": "На улице прошёл человек."}], seen)
    from vidette.assistant.tools import Tools

    assistant = Assistant(cfg, Tools(cfg, db), ledger=MemoryLedger(), http=http)
    reply = await assistant.answer("Кто это был?", event_id=event_id)
    assert reply.text == "На улице прошёл человек." and reply.model_calls == 2
    assert seen[1]["messages"][-1] == {"role": "user", "content": NUDGE}

    seen.clear()
    stubborn = fake_model([echo] * 4, seen)
    assistant = Assistant(cfg, Tools(cfg, db), ledger=MemoryLedger(), http=stubborn)
    reply = await assistant.answer("Кто это был?", event_id=event_id)
    assert "[tool_result]" not in reply.text and "{" not in reply.text and reply.text
    assert "passer_by" in reply.text, "a plain answer from the facts"
    await db.close()


async def test_it_was_me_a_test_is_feedback_and_the_other_camera_is_answered(
    tmp_path: Path,
) -> None:
    """A reply to an alert on a test deployment: right, staged, and what about the second
    camera: marked right as a test (never a 👎), and the incident answers."""
    from test_telegram import alert, bot, event

    b = await bot(tmp_path)
    linked_id = await event(b, "20260115-101400", state="dismissed", scenario="passer_by")
    event_id = await event(b, "20260115-101500")
    row = await b.db.get_event(event_id)
    assert row is not None
    record = {"event_id": linked_id, "camera": "front_door", "at": row.started_at - 60,
              "evidence": "look", "state": "dismissed", "label": "passer_by",
              "rationale": "Человек тянется к стене."}  # fmt: skip
    await b.db.update_event(
        event_id, intent=dict(row.intent or {}, incident={"role": "alert", "links": [record]})
    )
    [sent] = await alert(b, event_id)
    seen: list[dict[str, Any]] = []
    assert b.service._assistant is not None
    b.service._assistant._http = __import__("test_assistant").fake_model([], seen)
    question = {
        "update_id": 9,
        "message": {
            "message_id": 77, "date": int(b.clock[0]), "chat": {"id": sent.chat_id},
            "text": "Верно, это был я, проверка. А что было на второй камере?",
            "reply_to_message": {"message_id": sent.message_id},
        },
    }  # fmt: skip
    await b.service.handle_update(question)
    await asyncio.gather(*b.service._work)
    answer = b.fake.all("sendMessage")[-1].params["text"]
    assert answer.startswith("Отметил: верно, это проверка")
    assert "Похоже, тот же человек" in answer and "Человек тянется к стене" in answer
    assert "[tool_result]" not in answer and seen == [], "no model needed"
    marked = await b.db.get_event(event_id)
    assert marked is not None and marked.feedback == "up"
    assert marked.intent is not None and marked.intent["review"]["test"] is True
    await b.db.close()
