"""Questions in plain words (ADR-0017): the tools, the quick answers, the tool-calling loop
against a fake OpenAI-compatible server (native tool calls and the JSON-action fallback),
privacy for a cloud model, the bot's conversation, and the MCP endpoint."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_linked import T0, an_event, clip_at, config

import vidette.auth.deps as auth_deps
from vidette.api.routers.mcp import router as mcp_router
from vidette.assistant.agent import CAN_DO, Assistant
from vidette.assistant.tools import ToolError, Tools, parse_when
from vidette.auth.service import ANONYMOUS_ADMIN, Principal
from vidette.db import Database
from vidette.vlm.budget import MemoryLedger

NOW = T0.timestamp() + 3600


def model_config(tmp_path: Path, backend: str = "local") -> Any:
    cfg = config(tmp_path)
    return cfg.model_copy(
        update={
            "understanding": cfg.understanding.model_validate(
                {"vlm": {"backend": "local", "local": {"model": "fake", "base_url": "http://m/v1"},
                         "cloud": {"preset": "openai", "model": "fake", "api_key": "k"}}}
            ),
            "assistant": cfg.assistant.model_copy(update={"backend": backend}),
        }
    )  # fmt: skip


async def setup(tmp_path: Path) -> tuple[Database, Any, str]:
    cfg = model_config(tmp_path)
    db = Database(cfg.storage.database)
    await db.connect()
    own = clip_at(tmp_path / "b", "street", 100)
    other = clip_at(tmp_path / "c", "yard", 110)
    event_id = await an_event(db, own, "street", 100, identity=7)
    await an_event(db, other, "yard", 110, identity=7)
    return db, cfg, event_id


def test_times_in_plain_words() -> None:
    now = T0.timestamp()
    assert parse_when("12:00", now=now + 60) == now
    assert parse_when("-2h", now=now) == now - 7200
    assert parse_when("2026-11-14 12:00", now=now + 86400) == now
    try:
        parse_when("вчера вечером", now=now)
    except ToolError as exc:
        assert "HH:MM" in str(exc)
    else:
        raise AssertionError("an unreadable time is said plainly")


async def test_the_tools_find_events_clips_and_faces(tmp_path: Path) -> None:
    db, cfg, event_id = await setup(tmp_path)
    tools = Tools(cfg, db, camera_name=str.upper, clock=lambda: NOW)
    found = (await tools.call("find_events", {"since": "-3h"})).data
    assert found["total"] == 2 and found["events"][0]["cam"] == "YARD"
    assert found["events"][0]["faces"] == ["незнакомец №7"]
    by_face = (await tools.call("find_events", {"since": "-3h", "identity": 7})).data
    assert by_face["total"] == 2
    tools.current = event_id
    detail = (await tools.call("event_detail", {"event_id": "current"})).data
    assert detail["id"] == event_id and detail["what"] == "passer_by"
    around = (await tools.call("clips_at", {"time": "current"})).data
    yard = next(c for c in around["clips"] if c["cam"] == "YARD")
    assert yard["same_person_by_face"] is True
    assert around["nothing_from"] == ["DOOR"], "the camera that slept is named"
    try:
        await tools.call("mark_event", {"event_id": event_id, "answer": "right"})
    except ToolError as exc:
        assert "только из чата" in str(exc)
    await db.close()


def fake_model(answers: list[dict[str, Any]], seen: list[dict[str, Any]]) -> httpx.AsyncClient:
    queue = list(answers)

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        reply = queue.pop(0)
        if "status" in reply:
            return httpx.Response(reply["status"], text=reply["text"])
        return httpx.Response(200, json={"choices": [{"message": reply}]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


async def test_the_model_calls_tools_then_answers_briefly(tmp_path: Path) -> None:
    db, cfg, event_id = await setup(tmp_path)
    seen: list[dict[str, Any]] = []
    call = {"id": "c1", "type": "function",
            "function": {"name": "clips_at",
                         "arguments": json.dumps({"time": "current"})}}  # fmt: skip
    http = fake_model(
        [{"role": "assistant", "content": "", "tool_calls": [call]},
         {"role": "assistant", "content": "Во дворе в это же время был тот же человек."}],
        seen,
    )  # fmt: skip
    tools = Tools(cfg, db, clock=lambda: NOW)
    assistant = Assistant(cfg, tools, ledger=MemoryLedger(), http=http, clock=lambda: NOW)
    reply = await assistant.answer("Что видели остальные?", event_id=event_id)
    assert reply.text == "Во дворе в это же время был тот же человек." and reply.model_calls == 2
    first, second = seen
    assert first["tools"] and first["chat_template_kwargs"] == {"enable_thinking": False}
    assert "event_id current" in first["messages"][0]["content"]
    tool_turn = second["messages"][-1]
    assert tool_turn["role"] == "tool" and "same_person_by_face" in tool_turn["content"]
    await db.close()


async def test_a_server_without_tools_gets_a_json_action_prompt(tmp_path: Path) -> None:
    db, cfg, _event = await setup(tmp_path)
    seen: list[dict[str, Any]] = []
    http = fake_model(
        [{"status": 400, "text": "tools are not supported by this template"},
         {"role": "assistant", "content": '{"tool": "find_events", "args": {"since": "-3h"}}'},
         {"role": "assistant", "content": '{"answer": "Два события, оба прохожие."}'}],
        seen,
    )  # fmt: skip
    assistant = Assistant(cfg, Tools(cfg, db, clock=lambda: NOW), ledger=MemoryLedger(),
                          http=http, clock=lambda: NOW)  # fmt: skip
    reply = await assistant.answer("Что было за последние три часа?")
    assert reply.text == "Два события, оба прохожие."
    assert "tools" not in seen[1] and '"tool"' in seen[1]["messages"][0]["content"]
    assert seen[2]["messages"][-1]["content"].startswith("Результат find_events:")
    await db.close()


async def test_quick_answers_need_no_model(tmp_path: Path) -> None:
    db, cfg, event_id = await setup(tmp_path)
    own = await db.get_event(event_id)
    assert own is not None
    own_clip = own.source_ref
    http = fake_model([], [])  # any model call would fail the test
    days = []

    async def digest() -> str:
        days.append(1)
        return "Сегодня к 13:00\nТревог не было."

    sightings = [SimpleNamespace(at=T0.timestamp() + t, camera=c, face_px=60, source_id=ref)
                 for t, c, ref in ((110, "yard", "y"), (100, "street", str(own_clip)),
                                   (-3000, "door", "d"))]  # fmt: skip
    identities = SimpleNamespace(
        get=lambda n: SimpleNamespace(id=n, status="unknown", sightings=3, days=1, name=None),
        sightings=lambda n: sightings,
        resolve=lambda n: n,  # no merges here (ADR-0018)
    )
    tools = Tools(cfg, db, clock=lambda: NOW, digest=digest,
                  identities=identities)  # type: ignore[arg-type]  # fmt: skip
    assistant = Assistant(cfg, tools, ledger=MemoryLedger(), http=http, clock=lambda: NOW)
    where = await assistant.answer("Где ещё встречался этот человек?", event_id=event_id)
    assert where.fast and where.text.startswith("№7: всего 3 раза за 1 день"), where.text
    assert "yard" in where.text and "door" in where.text and "street" not in where.text
    tools.identities = None
    off = await assistant.answer("где ещё был этот человек", event_id=event_id)
    assert "выключены" in off.text, "said plainly, not invented"
    assert (await assistant.answer("что умеешь?")).text == CAN_DO
    today = await assistant.answer("Пришли сводку")
    assert today.fast and "Тревог не было" in today.text and days == [1]
    await db.close()


async def test_a_cloud_model_never_hears_a_name(tmp_path: Path) -> None:
    cfg = model_config(tmp_path, backend="cloud")
    db = Database(cfg.storage.database)
    await db.connect()
    own = clip_at(tmp_path / "b", "street", 100)
    event_id = await an_event(db, own, "street", 100)
    row = await db.get_event(event_id)
    assert row is not None and row.intent is not None
    row.intent["people"]["seen"] = [
        {"n": 1, "status": "known", "person": "sam", "name": "Sam", "score": 0.7},
        {"n": 2, "status": "unknown", "score": 0.2, "identity": {"id": 3, "name": "Соседка",
                                                                  "status": "familiar"}},
    ]  # fmt: skip
    await db.update_event(event_id, intent=row.intent)
    seen: list[dict[str, Any]] = []
    http = fake_model([{"role": "assistant", "content": "Двое: свой и знакомый №3."}], seen)
    tools = Tools(cfg, db, names=True, clock=lambda: NOW)
    assistant = Assistant(cfg, tools, ledger=MemoryLedger(), http=http, clock=lambda: NOW)
    await assistant.answer("Кто там был?", event_id=event_id)
    sent = json.dumps(seen, ensure_ascii=False)
    assert "свой" in sent and "знакомый №3" in sent
    assert "Sam" not in sent and "Соседка" not in sent
    await db.close()


def test_mcp_lists_and_calls_read_only_tools(tmp_path: Path) -> None:
    db, cfg, event_id = asyncio.run(setup(tmp_path))
    asyncio.run(db.close())

    app = FastAPI()
    app.include_router(mcp_router)
    runtime = SimpleNamespace(config=cfg, db=Database(cfg.storage.database), identities=None,
                              telegram=None)  # fmt: skip
    app.state.runtime = runtime
    app.dependency_overrides[auth_deps.current_principal] = lambda: ANONYMOUS_ADMIN
    with TestClient(app) as client:
        client.portal.call(runtime.db.connect)  # type: ignore[union-attr]

        def rpc(method: str, params: dict[str, Any] | None = None, rid: int = 1) -> Any:
            body = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
            return client.post("/api/v1/mcp", json=body).json()

        hello = rpc("initialize", {"protocolVersion": "2025-06-18"})
        assert hello["result"]["serverInfo"]["name"] == "vidette"
        notice = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        note = client.post("/api/v1/mcp", json=notice)
        assert note.status_code == 202
        names = [t["name"] for t in rpc("tools/list")["result"]["tools"]]
        assert "find_events" in names and "mark_event" not in names and "send_clips" not in names
        detail = rpc("tools/call", {"name": "event_detail", "arguments": {"event_id": event_id}})
        data = detail["result"]["structuredContent"]
        assert data["all_cameras_url"] == f"/api/v1/events/{event_id}/linked.mp4"
        refused = rpc("tools/call", {"name": "mark_event", "arguments": {}})
        assert refused["error"]["code"] == -32602
        assert client.get("/api/v1/mcp").status_code == 405
        viewer = Principal(1, "v", "viewer", frozenset({"read:streams"}), "token")
        app.dependency_overrides[auth_deps.current_principal] = lambda: viewer
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        assert client.post("/api/v1/mcp", json=ping).status_code == 403
        client.portal.call(runtime.db.close)  # type: ignore[union-attr]


async def test_the_bot_answers_a_reply_to_an_alert(tmp_path: Path) -> None:
    from test_telegram import alert, bot, event

    b = await bot(tmp_path)
    event_id = await event(b, "20260115-101500")
    [row] = await alert(b, event_id)
    seen: list[dict[str, Any]] = []
    http = fake_model([{"role": "assistant", "content": "Это был щиток, ничего страшного."}], seen)
    assert b.service._assistant is not None
    b.service._assistant._http = http
    question = {
        "update_id": 9,
        "message": {
            "message_id": 77, "date": int(b.clock[0]), "chat": {"id": row.chat_id},
            "text": "Что там происходило?", "reply_to_message": {"message_id": row.message_id},
        },
    }  # fmt: skip
    await b.service.handle_update(question)
    await asyncio.gather(*b.service._work)
    answer = b.fake.last("sendMessage").params
    assert answer["text"] == "Это был щиток, ничего страшного."
    assert answer["reply_parameters"]["message_id"] == 77
    assert event_id in json.dumps(seen[0]["messages"][0], ensure_ascii=False)
    stranger = dict(question, update_id=10)
    stranger["message"] = dict(question["message"], chat={"id": 9999})
    before = len(b.fake.calls)
    await b.service.handle_update(stranger)
    assert len(b.fake.calls) == before and not b.service._work, "strangers get nothing"
    await b.db.close()
