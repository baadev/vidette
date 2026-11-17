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
from html import escape
from typing import Any

import httpx

from vidette.assistant.tools import CURRENT, Media, Result, ToolError, Tools
from vidette.core.config import VidetteConfig, VlmBackend
from vidette.telegram.i18n import model_language, t
from vidette.telegram.text import plural
from vidette.vlm.budget import UsageLedger
from vidette.vlm.client import parse_json_answer
from vidette.vlm.endpoint import VlmEndpoint, VlmUnavailableError, endpoint_for
from vidette.vlm.gate import SlotGate

logger = logging.getLogger(__name__)


def can_do() -> str:
    """What the assistant can do, in the bot's language."""
    return t("assistant.can_do")


SYSTEM = (
    "You are the assistant of Vidette, a home camera system. Answer in {language}, briefly, "
    "in one to three sentences, only from what the tools return; never make anything up. "
    "If a camera recorded nothing, say so (battery cameras record only when they wake up). "
    "One person on several cameras only when a tool said same_person_by_face; otherwise say "
    '"at the same time", not "the same person". Never describe how people look. If the '
    "request is not about the cameras and events, say briefly what you can do: find "
    "events, send video from every camera, say where else a person was seen by face, the "
    "day's digest."
)
# The commonest questions, answered without the model; English and Russian are both
# understood whatever the bot's language.
_OTHER_CAMERAS = re.compile(
    r"друг\w*\s+камер|со?\s+всех\s+камер|все\s+камеры|остальн\w*\s+камер"
    r"|other\s+cameras|all\s+(the\s+)?cameras|every\s+camera"
)
_WHERE_ELSE = re.compile(
    r"где\s+(ещё|еще)|(ещё|еще)\s+(встреча|был|приход|появля)|раньше\s+(был|видел|приход)"
    r"|этот\s+человек|эта\s+женщина|этот\s+мужчина"
    r"|where\s+else|seen\s+(this|that)\s+(person|man|woman)|this\s+person\s+before"
)
_TODAY = re.compile(
    r"сводк|итог\w*\s+дня|что\s+было\s+сегодня|за\s+сегодня\??$"
    r"|digest|summary|what\s+happened\s+today"
)
_HELP = re.compile(r"^(что\s+(ты\s+)?умеешь|помощь|help|/help|what\s+can\s+you\s+do)\??$")
# "it was me, a test": the alert was right, and it was staged by one of us
_STAGED = re.compile(
    r"проверк|тест|симулир|инсцениров|специально|нарочно|понарошку"
    r"|\btest|\bdrill|staged|on\s+purpose|simulat|pretend"
)
_ITS_US = re.compile(
    r"это\s+(был[аи]?\s+)?(я|мы)\b|был[аи]?\s+(я|мы)\b|\bверно\b|\bправильно\b"
    r"|it\s+was\s+(me|us)|that\s+was\s+(me|us)|\bright\b|\bcorrect\b"
)
# the other cameras, also "the second camera" (how it was asked on a test deployment)
_OTHER_CAMERA = re.compile(
    r"втор\w*\s+камер|друг\w*\s+камер|соседн\w*\s+камер|other\s+camera|second\s+camera"
)
_ASKS = re.compile(r"\?|почему|что\s+|где\s+|когда|как\s+|не\s+вижу|why|what|where|when|how")
TOOL_RESULT_CHARS = 700  # a shared server's slot holds about 2000 tokens in all
# What a model sometimes writes instead of an answer: its tool call or a tool's result
# copied as text (17.11: «[tool_result]\n{"id": …}\n[/tool_result]» reached the chat).
_TRANSCRIPT = re.compile(
    r"\[(tool_result|tool_call|tool_response|function_call|function_result|tool)\]"
    r".*?(\[/\1\]|$)"
    r"|<(tool_call|tool_response|tool_result|function_call|function_result)>.*?(</\3>|$)"
    r"|<\|[a-z_]{1,40}\|>",
    re.S | re.I,
)
_FENCED_JSON = re.compile(r"```(?:json)?\s*[\[{].*?```", re.S)
_ECHOED_CALL = re.compile(
    r"(?:<tool_call>|\[tool_call\])\s*(\{.*?\})\s*(?:</tool_call>|\[/tool_call\]|$)", re.S
)
NUDGE = (
    "Answer my question now in one to three plain sentences, from what the tools returned. "
    "No JSON, no tool output, no markup."
)


def clean_answer(text: str) -> str:
    """The model's words without any tool transcript: tool calls or results it copied as
    text, special tokens, fenced or bare JSON, its thinking. Empty when nothing natural is
    left (then the answer is asked for again, or said plainly by the code)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = _TRANSCRIPT.sub("", text)
    text = _FENCED_JSON.sub("", text)
    stripped = text.strip()
    if stripped[:1] in ("{", "["):
        try:
            json.loads(stripped)
        except ValueError:
            if stripped.count('"') >= 4 and stripped.count(":") >= 2:
                return ""  # a cut-off JSON object is not an answer either
        else:
            return ""
    return stripped


def echoed_call(text: str) -> tuple[str, dict[str, Any]] | None:
    """A tool call the model wrote as text (`<tool_call>{"name": …, "arguments": …}`)
    instead of calling it: run it like a real one."""
    match = _ECHOED_CALL.search(text)
    if match is None:
        return None
    try:
        call = json.loads(match.group(1))
    except ValueError:
        return None
    if not isinstance(call, dict) or not isinstance(call.get("name"), str):
        return None
    args = call.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    return str(call["name"]), args if isinstance(args, dict) else {}


def staged_by_us(lowered: str) -> bool:
    """"It was me, a test": one of us staged it, and the alert was right."""
    return bool(_STAGED.search(lowered) and _ITS_US.search(lowered))


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
        lowered = text.strip().lower()
        prefix = ""
        if event_id and staged_by_us(lowered):
            # feedback first: right, staged by one of us (never a 👎 for calibration)
            try:
                await self.tools.call(
                    "mark_event", {"event_id": CURRENT, "answer": "test"}
                )
                prefix = t("assistant.marked_test")
            except ToolError as exc:
                prefix = str(exc)
        reply = await self._reply(text, lowered, event_id, marked=bool(prefix))
        if prefix:
            reply.text = f"{prefix}\n{reply.text}".strip()
        return reply

    async def _reply(self, text: str, lowered: str, event_id: str | None, *, marked: bool) -> Reply:
        if event_id and _OTHER_CAMERA.search(lowered):
            incident = await self._incident_reply()
            if incident is not None:
                return incident
        quick = await self._fast(text.strip(), event_id)
        if quick is not None:
            return quick
        if marked and not _ASKS.search(lowered):
            return Reply("", fast=True)  # only the feedback: nothing was asked
        endpoint = self.endpoint()
        if endpoint is None:
            return Reply(t("assistant.off") + can_do())
        try:
            return await self._converse(endpoint, text, event_id)
        except _ModelError as exc:
            logger.info("assistant: %s", exc)
            return Reply(t("assistant.model_failed", error=exc) + can_do())

    async def _incident_reply(self) -> Reply | None:
        """What the other cameras saw around this alert, from its incident links
        (ADR-0022), with their clips; None when the event has no incident record (the
        mosaic of every camera answers then)."""
        try:
            detail = (await self.tools.call("event_detail", {"event_id": CURRENT})).data
        except ToolError:
            return None
        links = detail.get("incident")
        if not isinstance(links, list):
            return None
        if not links:
            return Reply(t("assistant.incident_none"), fast=True)
        result = await self.tools.call("incident_clips", {"event_id": CURRENT})
        lines = [t("assistant.incident_head"), *(escape(str(link["line"])) for link in links)]
        return Reply("\n".join(lines), result.media, fast=True)

    async def _fast(self, text: str, event_id: str | None) -> Reply | None:
        """The commonest questions, answered without the model."""
        lowered = text.lower()
        if _HELP.search(lowered):
            return Reply(can_do(), fast=True)
        try:
            if event_id and _OTHER_CAMERAS.search(lowered):
                result = await self.tools.call("send_clips", {"event_id": CURRENT})
                if result.media:  # the caption under the video says it all
                    return Reply(t("assistant.all_cameras"), result.media, fast=True)
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
                line += t("assistant.time_approx")
            if clip.get("this_event"):
                line += ", " + t("linked.this_event")
            elif clip.get("same_person_by_face"):
                line += ", " + t("linked.same_person")
            elif clip.get("person"):
                line += ", " + t("linked.person_no_face")
            lines.append(line)
        if data.get("nothing_from"):
            lines.append(t("linked.nothing", names=", ".join(data["nothing_from"])))
        if not result.media:
            lines.insert(0, t("assistant.no_video"))
        return "\n".join(lines)

    async def _where_else(self) -> str:
        detail = (await self.tools.call("event_detail", {"event_id": CURRENT})).data
        numbers = [
            int(match.group(1))
            for face in detail.get("faces") or []
            if (match := re.search(r"[#№](\d+)", str(face)))
        ]
        if not numbers:
            if detail.get("faces"):
                return t("assistant.face_ours_or_small")
            return t("assistant.no_face")
        parts = []
        for number in numbers[:2]:
            found = (await self.tools.call("identity_sightings", {"identity": number})).data
            shown = found.get("shown") or []
            others = [s for s in shown if not s.get("this_event")]
            head = t("assistant.number", id=number) + (
                f" ({found['name']})" if found.get("name") else ""
            )
            if not others:
                parts.append(t("assistant.first_seen", head=head))
                continue
            listed = "; ".join(f"{s['t']} {s['cam']}" for s in others[:8])
            more = found["sightings_total"] - (len(shown) - len(others)) - len(others[:8])
            tail = t("assistant.and_more", n=more) if more > 0 else ""
            total = plural(int(found["sightings_total"]), "n.times")
            days = plural(int(found["days_seen"]), "n.days")
            parts.append(
                t("assistant.seen", head=head, total=total, days=days, listed=listed, tail=tail)
            )
        return "\n".join(parts)

    # --- the model -------------------------------------------------------------------------

    async def _converse(self, endpoint: VlmEndpoint, text: str, event_id: str | None) -> Reply:
        now = datetime.fromtimestamp(self._clock())
        system = SYSTEM.format(language=model_language()) + f" It is now {now:%Y-%m-%d %H:%M}."
        cameras = ", ".join(
            f"{f.camera} ({self.tools.camera_name(f.camera)})" for f in self.config.clip_folders
        )
        if cameras:
            system += f" Cameras: {cameras}."
        if event_id:
            brief = (await self.tools.call("event_detail", {"event_id": CURRENT})).data
            system += (
                " The question replies to this event (event_id current): "
                + json.dumps(brief, ensure_ascii=False)[:500]
            )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": text[:500]},
        ]
        media: list[Media] = []
        calls = 0
        nudged = False
        for step in range(self.config.assistant.max_steps + 1):
            final = step == self.config.assistant.max_steps
            message, used = await self._ask(endpoint, messages, final=final)
            calls += used
            requested = self._tool_requests(message)
            raw = self._text(message)
            if not requested and not final:
                echoed = echoed_call(str(message.get("content") or ""))
                if echoed is not None:  # a call written as text: run it all the same
                    requested = [(f"echo{step}", echoed[0], echoed[1])]
            if not requested or final:
                answer = clean_answer(raw)
                if answer:
                    return Reply(answer, media, model_calls=calls)
                if not final and not nudged:
                    # only a tool's call or result copied as text: once more, in words
                    nudged = True
                    messages.append({"role": "user", "content": NUDGE})
                    continue
                logger.info("assistant: the model gave no answer in words; a plain one")
                return Reply(await self._fallback(event_id), media, model_calls=calls)
            messages.append(self._assistant_turn(message, requested))
            for call_id, name, args in requested[:2]:
                try:
                    result = await self.tools.call(name, args)
                    media += result.media
                    content = json.dumps(result.data, ensure_ascii=False)
                except ToolError as exc:
                    content = json.dumps({"error": str(exc)}, ensure_ascii=False)
                messages.append(self._tool_turn(call_id, name, content[:TOOL_RESULT_CHARS]))
        return Reply(t("assistant.too_slow") + can_do(), media, model_calls=calls)

    async def _fallback(self, event_id: str | None) -> str:
        """A short plain answer from the facts when the model wrote none: what the event
        was and the other cameras' links. Never the model's echo."""
        if not event_id:
            return t("assistant.no_answer") + can_do()
        try:
            detail = (await self.tools.call("event_detail", {"event_id": CURRENT})).data
        except ToolError as exc:
            return str(exc)
        head = " · ".join(
            str(detail[key]) for key in ("cam", "t", "what") if detail.get(key)
        )
        lines = [head] if head else []
        if detail.get("description"):
            lines.append(str(detail["description"]))
        links = detail.get("incident")
        if isinstance(links, list) and links:
            lines += [str(link["line"]) for link in links if isinstance(link, dict)]
        return "\n".join(lines) or t("assistant.no_answer") + can_do()

    async def _ask(
        self, endpoint: VlmEndpoint, messages: list[dict[str, Any]], *, final: bool
    ) -> tuple[dict[str, Any], int]:
        backend = f"assistant-{endpoint.backend.value}"
        usage = await self._ledger.today(backend)
        if usage.calls >= self.config.assistant.max_calls_per_day:
            raise _ModelError(t("assistant.err.limit"))
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
            raise _ModelError(t("assistant.err.timeout")) from exc
        except httpx.HTTPError as exc:
            raise _ModelError(t("assistant.err.unreachable", name=type(exc).__name__)) from exc
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
                raise _ModelError(t("assistant.err.context"))
            raise _ModelError(f"HTTP {response.status_code}")
        try:
            message = response.json()["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise _ModelError(t("assistant.err.unclear")) from exc
        if not isinstance(message, dict):
            raise _ModelError(t("assistant.err.unclear"))
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
            "content": clean_answer(str(message.get("content") or "")),
            "tool_calls": [
                {"id": cid, "type": "function",
                 "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
                for cid, name, args in requested[:2]
            ],
        }  # fmt: skip

    def _tool_turn(self, call_id: str, name: str, content: str) -> dict[str, Any]:
        if not self._native_tools:
            return {"role": "user", "content": f"Result of {name}: {content}"}
        return {"role": "tool", "tool_call_id": call_id, "content": content}

    def _as_prompt(self, messages: list[dict[str, Any]], final: bool) -> list[dict[str, Any]]:
        tools = "; ".join(
            f"{s['name']}({', '.join(s['parameters'].get('properties', {}))}): {s['description']}"
            for s in self.tools.specs()
        )
        rule = (
            'Answer with JSON: {"answer": "text"}.'
            if final
            else 'Answer with one JSON object: {"tool": "name", "args": {…}} to call a tool, '
            'or {"answer": "text"} to answer. Tools: ' + tools
        )
        first = dict(messages[0])
        first["content"] = f"{first['content']}\n{rule}"
        return [first, *messages[1:]]


class _ModelError(Exception):
    pass
