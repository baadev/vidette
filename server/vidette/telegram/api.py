"""A small Telegram Bot API client: the dozen methods the bot uses, no framework.

- Long polling (`getUpdates`) only: the bot needs no webhook and no open port.
- `429 Too Many Requests` waits the `retry_after` Telegram asks for (capped) and retries;
  network errors and 5xx back off 1, 2, 4 s. A request that may already have been
  delivered (its answer was lost after it went out, as with a timeout after an upload)
  is not repeated: one alert must not become two. Every error says which it was
  (`TelegramError.unsent`, `.transient`), so the bot knows what it may send again later.
- No answer at all for `reconnect_after` (3 min): the HTTP client is replaced by a new
  one, with new connections and the system's DNS and proxy settings read again. A
  long-running service once failed to connect for over an hour while the machine itself
  reached Telegram, and a restart fixed it; this does what the restart did. Said once
  per outage in the log.
- The token is part of every URL, so nothing here ever puts a URL or an httpx exception
  text into an error message, and the `httpx` logger gets a filter that masks the token
  should anyone turn its request logging on.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

logger = logging.getLogger(__name__)

RETRY_DELAYS_S = (1.0, 2.0, 4.0)
MAX_RETRY_AFTER_S = 60.0
POLL_TIMEOUT_S = 50  # long poll: Telegram holds the request up to this long
RECONNECT_AFTER_S = 180.0  # no answer this long: a new HTTP client (module docstring)
# Failures that happen before Telegram could have read the request: it never acted on it.
# Any other network failure (a read timeout, a dropped connection) may come after it did.
_UNSENT = (
    httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.WriteError,
    httpx.WriteTimeout, httpx.ProxyError, httpx.UnsupportedProtocol, httpx.LocalProtocolError,
)  # fmt: skip


class TelegramError(Exception):
    """A failed Bot API call. The message names the method and Telegram's description,
    never the token. `unsent`: Telegram certainly did not act on the request (it answered
    with an error, or the request never reached it); False when the answer was lost after
    the request went out, so it may have arrived."""

    def __init__(
        self, method: str, description: str, *, code: int | None = None,
        retry_after: float | None = None, unsent: bool = True,
    ) -> None:  # fmt: skip
        super().__init__(f"telegram {method}: {description}")
        self.method = method
        self.description = description
        self.code = code
        self.retry_after = retry_after
        self.unsent = unsent

    @property
    def transient(self) -> bool:
        """Telegram did not take the request and may well take it later: no connection,
        `429` or a server error. Never true for a request that may have arrived: sending
        that one again could make one message two."""
        return self.unsent and (self.code is None or self.code == 429 or self.code >= 500)


def _network_words(exc: httpx.TransportError) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    return f"network error ({type(exc).__name__})"


class _MaskToken(logging.Filter):
    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self._token in message:
            record.msg = message.replace(self._token, "<telegram-token>")
            record.args = ()
        return True


def _mask_httpx_logs(token: str) -> None:
    for name in ("httpx", "httpcore"):
        target = logging.getLogger(name)
        if not any(isinstance(f, _MaskToken) and f._token == token for f in target.filters):
            target.addFilter(_MaskToken(token))


Upload = tuple[str, bytes, str]  # (file name, bytes, content type)


class TelegramApi:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.telegram.org",
        transport: httpx.AsyncBaseTransport | None = None,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        reconnect_after: float = RECONNECT_AFTER_S,
    ) -> None:
        """`transport` (tests): every client uses it; `transport_factory`: each new client
        gets its own. Neither: httpx's own, a new one for every client."""
        self._token = token
        self._base = f"{base_url.rstrip('/')}/bot{token}"
        self._new_transport: Callable[[], httpx.AsyncBaseTransport | None] = transport_factory or (
            lambda: transport
        )
        self._client = self._new_client()
        self._busy: dict[httpx.AsyncClient, int] = {}  # requests in flight, per client
        self._sleep = sleep
        self._clock = clock
        self._reconnect_after = reconnect_after
        self._silent_since: float | None = None  # first request without an answer
        self._reconnected_at: float | None = None
        self._said = False  # the outage is in the log
        self.reconnects = 0  # clients replaced since the start
        _mask_httpx_logs(token)

    def __repr__(self) -> str:
        return "TelegramApi(<token hidden>)"

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._new_transport(), timeout=30.0)

    async def aclose(self) -> None:
        for client in {self._client, *self._busy}:
            await client.aclose()

    async def reconnect(self) -> None:
        """A new HTTP client from now on. The old one is closed when its last request in
        flight (a long poll) is over, never under it."""
        old, self._client = self._client, self._new_client()
        self.reconnects += 1
        if old not in self._busy:
            with contextlib.suppress(Exception):
                await old.aclose()

    async def _post(
        self, method: str, params: dict[str, Any], files: dict[str, Upload] | None, timeout: float
    ) -> httpx.Response:
        client = self._client
        self._busy[client] = self._busy.get(client, 0) + 1
        try:
            if files:
                form = {
                    key: value if isinstance(value, str) else json.dumps(value)
                    for key, value in params.items()
                }
                return await client.post(
                    f"{self._base}/{method}", data=form, files=files, timeout=timeout
                )
            return await client.post(f"{self._base}/{method}", json=params, timeout=timeout)
        finally:
            left = self._busy.get(client, 1) - 1
            if left > 0:
                self._busy[client] = left
            else:
                self._busy.pop(client, None)
                if client is not self._client:  # replaced meanwhile: its last request
                    with contextlib.suppress(Exception):
                        await client.aclose()

    async def _no_answer(self) -> None:
        """A request got no answer at all. After `reconnect_after` of only that, and again
        every `reconnect_after` while it lasts: a new client."""
        now = self._clock()
        if self._silent_since is None:
            self._silent_since = now
        last = self._reconnected_at if self._reconnected_at is not None else self._silent_since
        if now - last < self._reconnect_after:
            return
        self._reconnected_at = now
        if not self._said:
            self._said = True
            logger.warning(
                "telegram: no answer for %.0f s; the HTTP client is replaced by a new one "
                "(new connections, DNS and proxy settings read again)",
                now - self._silent_since,
            )
        await self.reconnect()

    def _answered(self) -> None:
        if self._said and self._silent_since is not None and self._reconnected_at is not None:
            now = self._clock()
            logger.info(
                "telegram answers again after %.0f s without an answer, %.0f s after the "
                "last new client",
                now - self._silent_since,
                now - self._reconnected_at,
            )
        self._silent_since = self._reconnected_at = None
        self._said = False

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        files: dict[str, Upload] | None = None,
        timeout: float = 30.0,
        retry_timeouts: bool = True,
    ) -> Any:
        """One Bot API method; returns `result`. Raises TelegramError. With
        `retry_timeouts=False` a request that may have arrived (its answer was lost after
        it went out) is not repeated; one that never reached Telegram still is."""
        params = {key: value for key, value in (params or {}).items() if value is not None}
        attempt = 0
        while True:
            try:
                response = await self._post(method, params, files, timeout)
            except httpx.TransportError as exc:
                await self._no_answer()
                unsent = isinstance(exc, _UNSENT)
                if (not unsent and not retry_timeouts) or attempt >= len(RETRY_DELAYS_S):
                    raise TelegramError(method, _network_words(exc), unsent=unsent) from None
            else:
                self._answered()
                result = self._result(method, response)
                if not isinstance(result, TelegramError):
                    return result
                retryable = result.code == 429 or (result.code or 0) >= 500
                if not retryable or attempt >= len(RETRY_DELAYS_S):
                    raise result
                if result.retry_after is not None:
                    await self._sleep(min(result.retry_after, MAX_RETRY_AFTER_S))
                    attempt += 1
                    continue
            await self._sleep(RETRY_DELAYS_S[attempt])
            attempt += 1

    @staticmethod
    def _result(method: str, response: httpx.Response) -> Any:
        try:
            body = response.json()
        except ValueError:
            return TelegramError(
                method, f"HTTP {response.status_code}, not JSON", code=response.status_code
            )
        if isinstance(body, dict) and body.get("ok") is True:
            return body.get("result")
        description = "unknown error"
        retry_after = None
        if isinstance(body, dict):
            description = str(body.get("description") or description)[:300]
            parameters = body.get("parameters")
            if isinstance(parameters, dict) and isinstance(parameters.get("retry_after"), int):
                retry_after = float(parameters["retry_after"])
        code = body.get("error_code") if isinstance(body, dict) else None
        return TelegramError(
            method,
            description,
            code=code if isinstance(code, int) else response.status_code,
            retry_after=retry_after,
        )

    # --- the methods the bot uses ------------------------------------------------------

    async def get_me(self) -> dict[str, Any]:
        result: dict[str, Any] = await self.call("getMe")
        return result

    async def get_updates(self, offset: int | None, *, timeout: int = POLL_TIMEOUT_S) -> list[Any]:
        result = await self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": ["message", "callback_query"],
            },
            timeout=timeout + 15,
            retry_timeouts=False,
        )
        return list(result or [])

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
        reply_to: int | None = None,
        silent: bool = False,
        once: bool = False,
    ) -> dict[str, Any]:
        """`once`: never repeated when it may have arrived (TelegramApi.call)."""
        result: dict[str, Any] = await self.call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "message_thread_id": thread_id,
                "text": text,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
                "reply_markup": reply_markup,
                "reply_parameters": (
                    None
                    if reply_to is None
                    else {"message_id": reply_to, "allow_sending_without_reply": True}
                ),
                "disable_notification": silent or None,
            },
            retry_timeouts=not once,
        )
        return result

    async def send_media_group(
        self,
        chat_id: int,
        media: list[dict[str, Any]],
        files: dict[str, Upload],
        *,
        thread_id: int | None = None,
        silent: bool = False,
        timeout: float = 120.0,
    ) -> list[dict[str, Any]]:
        result = await self.call(
            "sendMediaGroup",
            {
                "chat_id": str(chat_id),
                "message_thread_id": None if thread_id is None else str(thread_id),
                "media": media,
                "disable_notification": "true" if silent else None,
            },
            files=files,
            timeout=timeout,
            retry_timeouts=False,  # the upload may have arrived: never send an album twice
        )
        return list(result or [])

    async def send_file(
        self,
        chat_id: int,
        kind: str,
        upload: Upload,
        *,
        thread_id: int | None = None,
        silent: bool = False,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        """One photo or video on its own (an album needs at least two)."""
        method, field = ("sendVideo", "video") if kind == "video" else ("sendPhoto", "photo")
        result: dict[str, Any] = await self.call(
            method,
            {
                "chat_id": str(chat_id),
                "message_thread_id": None if thread_id is None else str(thread_id),
                field: "attach://file",
                "supports_streaming": "true" if kind == "video" else None,
                "disable_notification": "true" if silent else None,
            },
            files={"file": upload},
            timeout=timeout,
            retry_timeouts=False,
        )
        return result

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        try:
            await self.call(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "link_preview_options": {"is_disabled": True},
                    "reply_markup": reply_markup,
                },
            )
        except TelegramError as exc:
            if "message is not modified" not in exc.description:
                raise

    async def edit_message_media(
        self,
        chat_id: int,
        message_id: int,
        kind: str,
        upload: Upload,
        *,
        timeout: float = 120.0,
    ) -> None:
        """Replace one message's photo or video (an album item may become a video)."""
        media: dict[str, Any] = {"type": kind, "media": "attach://file"}
        if kind == "video":
            media["supports_streaming"] = True
        await self.call(
            "editMessageMedia",
            {"chat_id": str(chat_id), "message_id": str(message_id), "media": media},
            files={"file": upload},
            timeout=timeout,
            retry_timeouts=False,
        )

    async def send_reply_file(
        self,
        chat_id: int,
        kind: str,
        upload: Upload,
        *,
        reply_to: int,
        thread_id: int | None = None,
        timeout: float = 120.0,
        caption: str | None = None,
        size: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        """A photo or video answering an earlier message, without a sound; `caption` is
        HTML, `size` (width, height) lets a phone show a video's shape before it loads."""
        method, field = ("sendVideo", "video") if kind == "video" else ("sendPhoto", "photo")
        result: dict[str, Any] = await self.call(
            method,
            {
                "chat_id": str(chat_id),
                "message_thread_id": None if thread_id is None else str(thread_id),
                field: "attach://file",
                "supports_streaming": "true" if kind == "video" else None,
                "disable_notification": "true",
                "reply_parameters": {"message_id": reply_to, "allow_sending_without_reply": True},
                "caption": caption,
                "parse_mode": "HTML" if caption else None,
                "width": None if size is None else str(size[0]),
                "height": None if size is None else str(size[1]),
            },
            files={"file": upload},
            timeout=timeout,
            retry_timeouts=False,
        )
        return result

    async def send_chat_action(
        self, chat_id: int, action: str = "typing", *, thread_id: int | None = None
    ) -> None:
        """ "typing…" or "sending a video…" while a slow answer is on its way."""
        with contextlib.suppress(TelegramError):
            await self.call(
                "sendChatAction",
                {"chat_id": chat_id, "action": action, "message_thread_id": thread_id},
            )

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        await self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    async def answer_callback_query(self, callback_id: str, text: str | None = None) -> None:
        try:
            await self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
        except TelegramError as exc:  # too old (the bot was down): the edit still happens
            logger.info("%s", exc)

    async def set_my_commands(self, commands: list[tuple[str, str]]) -> None:
        await self.call(
            "setMyCommands",
            {"commands": [{"command": name, "description": text} for name, text in commands]},
        )
