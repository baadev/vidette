"""A fake Telegram Bot API for the bot's tests: no network, no real bot, no real chats.

It answers the methods the bot uses the way Telegram documents them (`{"ok": true,
"result": …}`, `{"ok": false, "error_code": 429, "parameters": {"retry_after": 3}}`),
keeps every call (method, parameters, uploaded files) for assertions, and hands out
queued updates to `getUpdates` honouring `offset`. Uploads arrive as multipart and are
parsed with the standard library. Network trouble too: `offline` refuses every
connection (`httpx.ConnectError`, nothing arrives), `lose_answer(method)` lets the next
call of a method arrive and be kept, then loses its answer (`httpx.ReadTimeout`).

Use it in-process (`transport()`, an httpx transport) or as a real HTTP server on
127.0.0.1 (`serve()`) when the code under test builds its own client from a config.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import HTTP
from typing import Any

import httpx
import uvicorn

TOKEN = "123456789:test-token-not-a-real-one-0000000"


@dataclass
class Call:
    method: str
    params: dict[str, Any]
    files: dict[str, tuple[str, bytes]] = field(default_factory=dict)


@dataclass
class FakeTelegram:
    token: str = TOKEN
    calls: list[Call] = field(default_factory=list)
    updates: list[dict[str, Any]] = field(default_factory=list)
    errors: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # method → bodies
    next_id: int = 100
    offline: bool = False  # every connection is refused
    refused: int = 0  # requests refused while offline
    lost: list[str] = field(default_factory=list)  # methods whose next answer is lost

    def methods(self) -> list[str]:
        return [call.method for call in self.calls]

    def last(self, method: str) -> Call:
        return next(call for call in reversed(self.calls) if call.method == method)

    def all(self, method: str) -> list[Call]:
        return [call for call in self.calls if call.method == method]

    def lose_answer(self, method: str) -> None:
        self.lost.append(method)

    def fail(
        self, method: str, code: int, description: str, retry_after: int | None = None
    ) -> None:
        body: dict[str, Any] = {"ok": False, "error_code": code, "description": description}
        if retry_after is not None:
            body["parameters"] = {"retry_after": retry_after}
        self.errors.setdefault(method, []).append(body)

    # --- the API ------------------------------------------------------------------------

    def _message(self, params: dict[str, Any]) -> dict[str, Any]:
        self.next_id += 1
        return {
            "message_id": self.next_id,
            "date": int(time.time()),
            "chat": {"id": int(params.get("chat_id", 0)), "type": "private"},
            "text": params.get("text"),
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.offline:
            self.refused += 1
            raise httpx.ConnectError("connection refused", request=request)
        response = self._answer(request)
        method = request.url.path.rpartition("/")[2]
        if method in self.lost:
            self.lost.remove(method)
            raise httpx.ReadTimeout("the answer was lost", request=request)
        return response

    def _answer(self, request: httpx.Request) -> httpx.Response:
        prefix, _sep, method = request.url.path.rpartition("/")
        if prefix != f"/bot{self.token}":
            return httpx.Response(
                401, json={"ok": False, "error_code": 401, "description": "Unauthorized"}
            )
        params, files = _parse(request)
        self.calls.append(Call(method, params, files))
        queued = self.errors.get(method)
        if queued:
            body = queued.pop(0)
            return httpx.Response(int(body["error_code"]), json=body)
        result: Any
        if method == "getMe":
            result = {"id": 123456789, "is_bot": True, "username": "fake_vidette_bot"}
        elif method == "getUpdates":
            offset = int(params.get("offset") or 0)
            result = [u for u in self.updates if u["update_id"] >= offset]
            self.updates = []
        elif method in ("sendMessage", "sendPhoto", "sendVideo"):
            result = self._message(params)
        elif method == "sendMediaGroup":
            media = params.get("media")
            items = json.loads(media) if isinstance(media, str) else media
            result = [self._message(params) for _ in items]
        elif method in ("editMessageText", "answerCallbackQuery", "setMyCommands", "deleteMessage"):
            result = True
        elif method == "editMessageMedia":
            result = self._message(params)
        else:
            return httpx.Response(
                404, json={"ok": False, "error_code": 404, "description": "Not Found"}
            )
        return httpx.Response(200, json={"ok": True, "result": result})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @contextlib.contextmanager
    def serve(self) -> Iterator[str]:
        """Run on 127.0.0.1:<free port>; yields the base URL (the config's api_url)."""

        async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope["type"] != "http":
                return
            body = b""
            while True:
                message = await receive()
                body += message.get("body", b"")
                if not message.get("more_body"):
                    break
            headers = [(k.decode(), v.decode()) for k, v in scope["headers"]]
            request = httpx.Request(
                scope["method"], f"http://fake{scope['path']}", headers=headers, content=body
            )
            response = self.handle(request)
            await send(
                {
                    "type": "http.response.start",
                    "status": response.status_code,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": response.content})

        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("fake Telegram server did not start")
            time.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=10)


def _parse(request: httpx.Request) -> tuple[dict[str, Any], dict[str, tuple[str, bytes]]]:
    content_type = request.headers.get("content-type", "")
    body = request.read()
    if content_type.startswith("application/json"):
        return (json.loads(body) if body else {}), {}
    if content_type.startswith("multipart/form-data"):
        message = BytesParser(policy=HTTP).parsebytes(
            f"Content-Type: {content_type}\r\n\r\n".encode() + body
        )
        params: dict[str, Any] = {}
        files: dict[str, tuple[str, bytes]] = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename:
                files[str(name)] = (filename, payload)
            else:
                params[str(name)] = payload.decode()
        return params, files
    return {}, {}
