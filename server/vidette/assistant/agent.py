"""A question in plain words → the tools → a short answer in Russian, with videos.

    text ─┬─ the three commonest questions, recognized by their words: answered at once
          │    (other cameras at this moment · where else this person was · today)
          └─ anything else: the model with the tools, at most `max_steps` rounds
               (native tool calls; servers without them get the tools as a JSON-action
               prompt), then a short answer; an unsupported request is told what the
               assistant can do

The model is one of the judge's endpoints (`assistant.backend`, ADR-0017): the local one
by default. A cloud model never sees a name (results are made without names), a face or
a frame. Every call counts against `assistant.max_calls_per_day`; failures become a short
honest answer, never an exception in the chat.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from vidette.assistant.tools import CURRENT, Media, Result, ToolError, Tools
from vidette.core.config import VidetteConfig, VlmBackend
from vidette.telegram.text import plural
from vidette.vlm.budget import UsageLedger
from vidette.vlm.client import parse_json_answer
from vidette.vlm.endpoint import VlmEndpoint, VlmUnavailableError, endpoint_for
from vidette.vlm.gate import SlotGate

logger = logging.getLogger(__name__)

CAN_DO = (
    "Я умею: присылать видео со всех камер в момент тревоги («пришли другие камеры» в ответ "
    "на тревогу), искать, где ещё был человек по лицу («где ещё был этот человек?»), "
    "находить события («что было на улице после 12:00?», «тревоги за сегодня»), "
    "рассказывать подробности события и сводку дня, отмечать тревогу верной или ложной."
)
SYSTEM = (
    "Ты помощник домашней системы видеонаблюдения Vidette. Отвечай по-русски, коротко, "
    "одним-тремя предложениями, только по данным инструментов; ничего не выдумывай. "
    "Если камера ничего не записала, так и скажи (камеры на батарейках пишут, только когда "
    "проснулись). Один человек на разных камерах — только если инструмент сказал "
    "same_person_by_face; иначе говори «в это же время», а не «тот же человек». Никогда не "
    "описывай внешность людей. Если просьба не про камеры и события, коротко скажи, что "
    "умеешь: найти события, прислать видео со всех камер, сказать, где ещё был человек по "
    "лицу, сводку дня."
)
_OTHER_CAMERAS = re.compile(r"друг\w*\s+камер|со?\s+всех\s+камер|все\s+камеры|остальн\w*\s+камер")
_WHERE_ELSE = re.compile(
    r"где\s+(ещё|еще)|(ещё|еще)\s+(встреча|был|приход|появля)|раньше\s+(был|видел|приход)"
    r"|этот\s+человек|эта\s+женщина|этот\s+мужчина"
)
_TODAY = re.compile(r"сводк|итог\w*\s+дня|что\s+было\s+сегодня|за\s+сегодня\??$")
_HELP = re.compile(r"^(что\s+(ты\s+)?умеешь|помощь|help|/help)\??$")
TOOL_RESULT_CHARS = 700  # a shared server's slot holds about 2000 tokens in all


@dataclass
class Reply:
    text: str
    media: list[Media] = field(default_factory=list)
    model_calls: int = 0
    fast: bool = False  # answered without the model


class Assistant:
    def __init__(
        self,
        config: VidetteConfig,
        tools: Tools,
        *,
        ledger: UsageLedger,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
        gate_for: Callable[[VlmEndpoint], SlotGate] | None = None,
    ) -> None:
        self.config = config
        self.tools = tools
        self._gate_for = gate_for  # the shared model slot (vidette.vlm.gate), live priority
        self._ledger = ledger
        self._http = http or httpx.AsyncClient()
        self._clock = clock
        self._native_tools = True  # until the server says it has none

    async def aclose(self) -> None:
        await self._http.aclose()

    def endpoint(self) -> VlmEndpoint | None:
        backend = self.config.assistant.backend
        if not self.config.assistant.enabled or backend == "none":
            return None
        try:
            return endpoint_for(self.config, VlmBackend(backend))
        except VlmUnavailableError:
            return None

    # --- the answer ------------------------------------------------------------------------

    async def answer(self, text: str, *, event_id: str | None = None) -> Reply:
        self.tools.current = event_id
        self.tools.names = self.tools.names and self.config.assistant.backend == "local"
        quick = await self._fast(text.strip(), event_id)
        if quick is not None:
            return quick
        endpoint = self.endpoint()
        if endpoint is None:
            return Reply("Свободные вопросы выключены (assistant.backend: none). " + CAN_DO)
        try:
            return await self._converse(endpoint, text, event_id)
        except _ModelError as exc:
            logger.info("assistant: %s", exc)
            return Reply(f"Модель не ответила ({exc}). Попробуйте позже. " + CAN_DO)

    async def _fast(self, text: str, event_id: str | None) -> Reply | None:
        """The commonest questions, answered without the model."""
        lowered = text.lower()
        if _HELP.search(lowered):
            return Reply(CAN_DO, fast=True)
        try:
            if event_id and _OTHER_CAMERAS.search(lowered):
                result = await self.tools.call("send_clips", {"event_id": CURRENT})
                if result.media:  # the caption under the video says it all
                    return Reply("Все камеры в этот момент, одним видео:", result.media, fast=True)
                return Reply(self._clips_text(result), fast=True)
            if event_id and _WHERE_ELSE.search(lowered):
                return Reply(await self._where_else(), fast=True)
            if _TODAY.search(lowered):
                result = await self.tools.call("today", {})
                return Reply(str(result.data.get("digest") or ""), fast=True)
        except ToolError as exc:
            return Reply(str(exc), fast=True)
        return None

    def _clips_text(self, result: Result) -> str:
        data = result.data
        lines = []
        for clip in data.get("clips") or []:
            line = f"{clip['cam']}: {clip['from']}–{clip['to']}"
            if clip.get("time"):
                line += " (время примерное)"
            if clip.get("this_event"):
                line += ", это событие"
            elif clip.get("same_person_by_face"):
                line += ", тот же человек по лицу"
            elif clip.get("person"):
                line += ", человек в кадре, лицо не сравнить"
            lines.append(line)
        if data.get("nothing_from"):
            lines.append(
                f"{', '.join(data['nothing_from'])}: в это время ничего не записано "
                "(камера спала или движения не было)"
            )
        if not result.media:
            lines.insert(0, "Видео нет: в этот момент записала только эта камера.")
        return "\n".join(lines)

    async def _where_else(self) -> str:
        detail = (await self.tools.call("event_detail", {"event_id": CURRENT})).data
        numbers = [
            int(match.group(1))
            for face in detail.get("faces") or []
            if (match := re.search(r"№(\d+)", str(face)))
        ]
        if not numbers:
            if detail.get("faces"):
                return "Лицо здесь распознано как своё или слишком мелкое: сравнивать не с чем."
            return (
                "В этом событии лицо не найдено, поэтому сравнить человека с другими записями "
                "нельзя. По одежде или росту я людей не сравниваю. Могу прислать, что "
                "записали другие камеры в этот момент."
            )
        parts = []
        for number in numbers[:2]:
            found = (await self.tools.call("identity_sightings", {"identity": number})).data
            shown = found.get("shown") or []
            others = [s for s in shown if not s.get("this_event")]
            head = f"№{number}" + (f" ({found['name']})" if found.get("name") else "")
            if not others:
                parts.append(f"{head}: других встреч за неделю нет, это первая.")
                continue
            listed = "; ".join(f"{s['t']} {s['cam']}" for s in others[:8])
            more = found["sightings_total"] - (len(shown) - len(others)) - len(others[:8])
            tail = f" и ещё {more}" if more > 0 else ""
            total = plural(int(found["sightings_total"]), "раз", "раза", "раз")
            days = plural(int(found["days_seen"]), "день", "дня", "дней")
            parts.append(f"{head}: всего {total} за {days}; другие встречи: {listed}{tail}.")
        return "\n".join(parts)

    # --- the model -------------------------------------------------------------------------

    async def _converse(self, endpoint: VlmEndpoint, text: str, event_id: str | None) -> Reply:
        now = datetime.fromtimestamp(self._clock())
        system = SYSTEM + f" Сейчас {now:%Y-%m-%d %H:%M}."
        cameras = ", ".join(
            f"{f.camera} ({self.tools.camera_name(f.camera)})" for f in self.config.clip_folders
        )
        if cameras:
            system += f" Камеры: {cameras}."
        if event_id:
            brief = (await self.tools.call("event_detail", {"event_id": CURRENT})).data
            system += (
                " Вопрос задан в ответ на это событие (event_id current): "
                + json.dumps(brief, ensure_ascii=False)[:500]
            )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": text[:500]},
        ]
        media: list[Media] = []
        calls = 0
        for step in range(self.config.assistant.max_steps + 1):
            final = step == self.config.assistant.max_steps
            message, used = await self._ask(endpoint, messages, final=final)
            calls += used
            requested = self._tool_requests(message)
            if not requested or final:
                answer = self._text(message) or "Не нашёл, что ответить. " + CAN_DO
                return Reply(answer.strip(), media, model_calls=calls)
            messages.append(self._assistant_turn(message, requested))
            for call_id, name, args in requested[:2]:
                try:
                    result = await self.tools.call(name, args)
                    media += result.media
                    content = json.dumps(result.data, ensure_ascii=False)
                except ToolError as exc:
                    content = json.dumps({"error": str(exc)}, ensure_ascii=False)
                messages.append(self._tool_turn(call_id, name, content[:TOOL_RESULT_CHARS]))
        return Reply("Не успел ответить. " + CAN_DO, media, model_calls=calls)

    async def _ask(
        self, endpoint: VlmEndpoint, messages: list[dict[str, Any]], *, final: bool
    ) -> tuple[dict[str, Any], int]:
        backend = f"assistant-{endpoint.backend.value}"
        usage = await self._ledger.today(backend)
        if usage.calls >= self.config.assistant.max_calls_per_day:
            raise _ModelError("дневной лимит вопросов исчерпан")
        body: dict[str, Any] = {
            "model": endpoint.model,
            "messages": messages if self._native_tools else self._as_prompt(messages, final),
            endpoint.token_param: 250,  # answers are short; the slot is small
            "temperature": 0.2,
        }
        if self._native_tools and not final:
            body["tools"] = [{"type": "function", "function": s} for s in self.tools.specs()]
        if endpoint.backend is VlmBackend.local:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        headers = {"content-type": "application/json"}
        if endpoint.api_key:
            headers["authorization"] = f"Bearer {endpoint.api_key}"
        await self._ledger.add(backend, calls=1, usd=0.0)
        gate = self._gate_for(endpoint) if self._gate_for is not None else None
        try:
            async with gate.live() if gate is not None else contextlib.nullcontext():
                response = await self._http.post(
                    f"{endpoint.base_url}/chat/completions", json=body, headers=headers,
                    timeout=self.config.assistant.timeout_s,
                )  # fmt: skip
        except httpx.TimeoutException as exc:
            raise _ModelError("не уложилась во время") from exc
        except httpx.HTTPError as exc:
            raise _ModelError(f"недоступна: {type(exc).__name__}") from exc
        if response.status_code == 400 and self._native_tools and "tool" in response.text.lower():
            self._native_tools = False  # this server has no tool calls: the JSON prompt
            message, used = await self._ask(endpoint, messages, final=final)
            return message, used + 1
        if not response.is_success:
            detail = response.text.strip().replace("\n", " ")[:200]
            if endpoint.api_key:
                detail = detail.replace(endpoint.api_key, "***")
            logger.info("assistant: %s answered HTTP %d: %s", endpoint.host, response.status_code,
                        detail)  # fmt: skip
            if "context" in detail.lower():
                raise _ModelError("вопрос и найденное не поместились в модель")
            raise _ModelError(f"HTTP {response.status_code}")
        try:
            message = response.json()["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise _ModelError("непонятный ответ") from exc
        if not isinstance(message, dict):
            raise _ModelError("непонятный ответ")
        return message, 1

    # --- the two dialects: native tool calls, and a JSON action in plain text --------------

    def _tool_requests(self, message: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
        found: list[tuple[str, str, dict[str, Any]]] = []
        for n, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict):
                continue
            try:
                args = json.loads(function.get("arguments") or "{}")
            except ValueError:
                args = {}
            found.append((str(call.get("id") or f"call{n}"), str(function.get("name")), args))
        if found or self._native_tools:
            return found
        try:  # the JSON-action dialect: {"tool": …, "args": {…}} or {"answer": …}
            action = parse_json_answer(self._text(message))
        except Exception:
            return []
        if isinstance(action.get("tool"), str):
            raw = action.get("args")
            return [("json", str(action["tool"]), raw if isinstance(raw, dict) else {})]
        return []

    def _text(self, message: dict[str, Any]) -> str:
        content = message.get("content")
        text = content if isinstance(content, str) else ""
        if not self._native_tools and text.strip().startswith("{"):
            try:
                action = parse_json_answer(text)
            except Exception:
                return text
            if isinstance(action.get("answer"), str):
                return str(action["answer"])
        return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()

    def _assistant_turn(
        self, message: dict[str, Any], requested: list[tuple[str, str, dict[str, Any]]]
    ) -> dict[str, Any]:
        if not self._native_tools:
            return {"role": "assistant", "content": self._text(message) or json.dumps(
                {"tool": requested[0][1], "args": requested[0][2]}, ensure_ascii=False
            )}  # fmt: skip
        return {
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": [
                {"id": cid, "type": "function",
                 "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
                for cid, name, args in requested[:2]
            ],
        }  # fmt: skip

    def _tool_turn(self, call_id: str, name: str, content: str) -> dict[str, Any]:
        if not self._native_tools:
            return {"role": "user", "content": f"Результат {name}: {content}"}
        return {"role": "tool", "tool_call_id": call_id, "content": content}

    def _as_prompt(self, messages: list[dict[str, Any]], final: bool) -> list[dict[str, Any]]:
        tools = "; ".join(
            f"{s['name']}({', '.join(s['parameters'].get('properties', {}))}): {s['description']}"
            for s in self.tools.specs()
        )
        rule = (
            'Ответь JSON: {"answer": "текст"}.'
            if final
            else 'Ответь одним JSON: {"tool": "имя", "args": {…}} чтобы вызвать инструмент, '
            'или {"answer": "текст"} чтобы ответить. Инструменты: ' + tools
        )
        first = dict(messages[0])
        first["content"] = f"{first['content']}\n{rule}"
        return [first, *messages[1:]]


class _ModelError(Exception):
    pass
