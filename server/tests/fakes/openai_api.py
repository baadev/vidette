"""A fake OpenAI-compatible chat API (`POST /v1/chat/completions`) for VLM tests.

It checks requests the way strict servers do (model, messages, image data URLs) and
answers from a script, so tests can play both a local Ollama and a hosted provider:
different answers, latencies, token counts, a server that rejects `json_schema`, one that
wraps JSON in code fences, one that times out or refuses the key. No real model, no
network beyond localhost, no money.

Use it in-process (`transport()`, an httpx ASGI transport) or as a real HTTP server on
127.0.0.1 (`serve()`, uvicorn in a thread) when the code under test builds its own client.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

Answer = Callable[[str, list[bytes], dict[str, Any]], dict[str, Any]]


def default_answer(text: str, images: list[bytes], body: dict[str, Any]) -> dict[str, Any]:
    return {"decision": "silent", "label": "nothing", "description": "Nothing.", "confidence": 0.5}


@dataclass
class FakeVlmApi:
    answer: Answer = default_answer
    model: str = "fake-vlm"
    api_key: str | None = None  # require "Bearer <key>" when set
    latency_s: float = 0.0
    reject_json_schema: bool = False  # like servers without structured outputs
    fenced: bool = False  # wrap the JSON in ```json fences, like chatty models
    force_status: int | None = None
    force_body: str = ""
    force_count: int | None = None  # force only the first N requests (None: all)
    usage: tuple[int, int] = (1000, 40)
    requests: list[dict[str, Any]] = field(default_factory=list)
    # llama.cpp's GET /slots: one bool per slot, True when it is processing; None: 404
    slots: list[bool] | None = None
    slot_reads: int = 0

    def app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/slots")
        async def slots(request: Request) -> JSONResponse:
            self.slot_reads += 1
            if self.api_key is not None and request.headers.get("authorization") != (
                f"Bearer {self.api_key}"
            ):
                return JSONResponse({"error": {"message": "invalid api key"}}, status_code=401)
            if self.slots is None:
                return JSONResponse({"error": {"message": "not found"}}, status_code=404)
            return JSONResponse(
                [
                    {"id": n, "n_ctx": 2048, "is_processing": busy}
                    for n, busy in enumerate(self.slots)
                ]
            )

        @app.post("/v1/chat/completions")
        async def completions(request: Request) -> JSONResponse:
            body = await request.json()
            self.requests.append(
                {"body": body, "authorization": request.headers.get("authorization")}
            )
            if self.api_key is not None and request.headers.get("authorization") != (
                f"Bearer {self.api_key}"
            ):
                return JSONResponse({"error": {"message": "invalid api key"}}, status_code=401)
            if self.force_status is not None and (
                self.force_count is None or len(self.requests) <= self.force_count
            ):
                return JSONResponse(
                    {"error": {"message": self.force_body or "forced"}},
                    status_code=self.force_status,
                )
            if body.get("model") != self.model:
                return JSONResponse(
                    {"error": {"message": f'model "{body.get("model")}" not found'}},
                    status_code=404,
                )
            response_format = body.get("response_format") or {}
            if self.reject_json_schema and response_format.get("type") == "json_schema":
                return JSONResponse(
                    {"error": {"message": "response_format json_schema is not supported"}},
                    status_code=400,
                )
            text, images = "", []
            for message in body["messages"]:
                content = message["content"]
                if isinstance(content, str):
                    continue
                for part in content:
                    if part["type"] == "text":
                        text += part["text"]
                    elif part["type"] == "image_url":
                        url = part["image_url"]["url"]
                        assert url.startswith("data:image/jpeg;base64,"), url[:40]
                        images.append(base64.b64decode(url.split(",", 1)[1]))
            if self.latency_s:
                await asyncio.sleep(self.latency_s)
            content_text = json.dumps(self.answer(text, images, body))
            if self.fenced:
                content_text = f"Here you go:\n```json\n{content_text}\n```"
            prompt_tokens, completion_tokens = self.usage
            return JSONResponse(
                {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion",
                    "model": self.model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content_text},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": prompt_tokens + 250 * len(images),
                        "completion_tokens": completion_tokens,
                    },
                }
            )

        return app

    def transport(self) -> httpx.ASGITransport:
        return httpx.ASGITransport(app=self.app())

    @contextlib.contextmanager
    def serve(self) -> Iterator[str]:
        """Run on 127.0.0.1:<free port>; yields the base URL ending in /v1."""
        config = uvicorn.Config(self.app(), host="127.0.0.1", port=0, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("fake VLM server did not start")
            time.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        try:
            yield f"http://127.0.0.1:{port}/v1"
        finally:
            server.should_exit = True
            thread.join(timeout=10)
