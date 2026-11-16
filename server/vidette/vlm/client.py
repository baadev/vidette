"""One OpenAI-compatible chat client for every VLM backend (Ollama, llama.cpp, OpenAI,
Gemini's OpenAI endpoint, OpenRouter, anything else that speaks `/chat/completions`).

A call sends a system prompt, a text prompt and a few JPEG frames as data URLs, asks for
an answer that follows a JSON schema, and returns the parsed object with what it cost:
latency, tokens and (with prices configured) dollars. Not every server supports
`response_format: json_schema`; on a 400 that names it, the client falls back to
`json_object`, then to plain instructions, and remembers what worked.

Limits are enforced here, before anything leaves the machine: the daily call budget
(hard stop, the judge abstains) and the per-minute pace (callers wait). Every request
sent counts against the budget, including failed ones, because providers may bill them.

Errors are `VlmError` with a `kind` and a message that says what to do; neither ever
contains the API key.
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from vidette.vlm.budget import Pace, UsageLedger
from vidette.vlm.endpoint import VlmEndpoint
from vidette.vlm.gate import SlotGate

ErrorKind = Literal[
    "budget", "timeout", "network", "auth", "not_found", "rate_limited", "server", "bad_answer"
]
FormatMode = Literal["json_schema", "json_object", "prompt"]
_FALLBACK: dict[FormatMode, FormatMode | None] = {
    "json_schema": "json_object",
    "json_object": "prompt",
    "prompt": None,
}
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class VlmError(Exception):
    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class VlmAnswer:
    data: dict[str, Any]
    latency_s: float
    tokens_in: int | None
    tokens_out: int | None
    usd: float | None  # None: a cloud backend without prices (unknown, not free)
    images: int
    model: str
    calls: int  # requests sent for this answer (format fallbacks add one each)


def parse_json_answer(content: str) -> dict[str, Any]:
    """The model's text as a JSON object: tolerant of code fences and chatter around it."""
    text = _FENCE_RE.sub("", content.strip()).strip()
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise VlmError("bad_answer", f"the model did not answer with a JSON object: {text[:120]!r}")


def _content_text(message: Any) -> str:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # some servers return content parts
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def estimate_usd(
    endpoint: VlmEndpoint, *, tokens_in: int | None, tokens_out: int | None, images: int
) -> float | None:
    prices = endpoint.prices
    if not prices.is_set:
        return 0.0 if endpoint.backend == "local" else None
    return (
        prices.usd_per_call
        + images * prices.usd_per_image
        + (tokens_in or 0) * prices.usd_per_mtok_in / 1e6
        + (tokens_out or 0) * prices.usd_per_mtok_out / 1e6
    )


class VlmClient:
    def __init__(
        self,
        endpoint: VlmEndpoint,
        *,
        ledger: UsageLedger,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        gate: SlotGate | None = None,
        background: bool = False,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.gate = gate  # the one shared slot (vidette.vlm.gate); None: no sharing
        self.background = background  # yields the slot to live calls (deep analysis)
        self._extra_body = dict(extra_body or {})
        self._ledger = ledger
        self._http = http or httpx.AsyncClient()
        self._owns_http = http is None
        self._clock = clock
        self._pace = Pace(endpoint.max_calls_per_minute)
        self._mode: FormatMode = "json_schema"

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def _scrub(self, text: str) -> str:
        key = self.endpoint.api_key
        return text.replace(key, "***") if key else text

    async def ask(
        self,
        *,
        system: str,
        text: str,
        images: list[bytes],
        schema: dict[str, Any],
        schema_name: str,
    ) -> VlmAnswer:
        endpoint = self.endpoint
        backend = endpoint.backend.value
        usage = await self._ledger.today(backend)
        if usage.calls >= endpoint.max_calls_per_day:
            raise VlmError(
                "budget",
                f"daily VLM budget reached ({usage.calls}/{endpoint.max_calls_per_day} calls "
                f"for {backend} today); raise max_calls_per_day or wait until tomorrow",
            )
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for image in images:
            data_url = "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": data_url}})

        if self.gate is None:
            return await self._ask(system, text, content, images, schema, schema_name)
        hold = self.gate.background() if self.background else self.gate.live()
        async with hold:
            return await self._ask(system, text, content, images, schema, schema_name)

    async def _ask(
        self,
        system: str,
        text: str,
        content: list[dict[str, Any]],
        images: list[bytes],
        schema: dict[str, Any],
        schema_name: str,
    ) -> VlmAnswer:
        endpoint = self.endpoint
        backend = endpoint.backend.value
        calls = 0
        started = self._clock()
        mode: FormatMode | None = self._mode
        while mode is not None:
            body: dict[str, Any] = {
                "model": endpoint.model,
                "messages": [
                    {"role": "system", "content": system + _mode_hint(mode, schema)},
                    {"role": "user", "content": content},
                ],
                endpoint.token_param: endpoint.max_output_tokens,
                **self._extra_body,
            }
            if mode == "json_schema":
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "schema": schema, "strict": True},
                }
            elif mode == "json_object":
                body["response_format"] = {"type": "json_object"}
            await self._pace.wait()
            calls += 1
            try:
                response = await self._post(body)
            finally:
                await self._ledger.add(backend, calls=1, usd=0.0)
            if response.status_code == 400 and _format_rejected(response.text):
                mode = _FALLBACK[mode]
                continue
            self._raise_for_status(response)
            self._mode = mode
            break
        else:
            raise VlmError("server", f"{endpoint.host} rejected every response format")

        latency = self._clock() - started
        try:
            payload = response.json()
            message = payload["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise VlmError(
                "bad_answer", f"{endpoint.host} returned no chat completion: {exc}"
            ) from exc
        usage_block = payload.get("usage") or {}
        tokens_in = usage_block.get("prompt_tokens")
        tokens_out = usage_block.get("completion_tokens")
        usd = estimate_usd(
            endpoint,
            tokens_in=tokens_in if isinstance(tokens_in, int) else None,
            tokens_out=tokens_out if isinstance(tokens_out, int) else None,
            images=len(images),
        )
        if usd:
            await self._ledger.add(backend, calls=0, usd=usd)
        data = parse_json_answer(_content_text(message))
        return VlmAnswer(
            data=data,
            latency_s=round(latency, 3),
            tokens_in=tokens_in if isinstance(tokens_in, int) else None,
            tokens_out=tokens_out if isinstance(tokens_out, int) else None,
            usd=usd,
            images=len(images),
            model=str(payload.get("model") or endpoint.model),
            calls=calls,
        )

    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        endpoint = self.endpoint
        headers = {"content-type": "application/json"}
        if endpoint.api_key:
            headers["authorization"] = f"Bearer {endpoint.api_key}"
        try:
            return await self._http.post(
                f"{endpoint.base_url}/chat/completions",
                json=body,
                headers=headers,
                timeout=endpoint.timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise VlmError(
                "timeout", f"{endpoint.host} did not answer within {endpoint.timeout_s:g} s"
            ) from exc
        except httpx.HTTPError as exc:
            hint = " (is Ollama running? `ollama serve`)" if endpoint.backend == "local" else ""
            raise VlmError(
                "network", self._scrub(f"cannot reach {endpoint.host}: {exc}{hint}")
            ) from exc

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        endpoint = self.endpoint
        status = response.status_code
        detail = self._scrub(response.text.strip().replace("\n", " ")[:200])
        if status in (401, 403):
            raise VlmError("auth", f"{endpoint.host} rejected the API key (HTTP {status})")
        if status == 404:
            hint = (
                f"; run `ollama pull {endpoint.model}`"
                if endpoint.backend == "local"
                else "; check the model name against the provider's list"
            )
            raise VlmError(
                "not_found", f"{endpoint.host}: model '{endpoint.model}' not found{hint}"
            )
        if status == 429:
            raise VlmError("rate_limited", f"{endpoint.host} is rate limiting (HTTP 429)")
        raise VlmError("server", f"{endpoint.host} answered HTTP {status}: {detail}")


def _format_rejected(body: str) -> bool:
    lowered = body.lower()
    return any(word in lowered for word in ("response_format", "json_schema", "schema"))


def _mode_hint(mode: FormatMode, schema: dict[str, Any]) -> str:
    if mode == "json_schema":
        return ""
    return (
        "\n\nAnswer with one JSON object only, no prose, matching this JSON schema:\n"
        + json.dumps(schema)
    )
