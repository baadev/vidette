"""A small Telegram Bot API client: the dozen methods the bot uses, no framework.

- Long polling (`getUpdates`) only: the bot needs no webhook and no open port.
- `429 Too Many Requests` waits the `retry_after` Telegram asks for (capped) and retries;
  network errors and 5xx back off 1, 2, 4 s. A request that may already have been
  delivered (a timeout after an upload) is not repeated: one alert must not become two.
- The token is part of every URL, so nothing here ever puts a URL or an httpx exception
  text into an error message, and the `httpx` logger gets a filter that masks the token
  should anyone turn its request logging on.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

logger = logging.getLogger(__name__)

RETRY_DELAYS_S = (1.0, 2.0, 4.0)
MAX_RETRY_AFTER_S = 60.0
POLL_TIMEOUT_S = 50  # long poll: Telegram holds the request up to this long


class TelegramError(Exception):
    """A failed Bot API call. The message names the method and Telegram's description,
    never the token."""

    def __init__(
        self, method: str, description: str, *, code: int | None = None,
        retry_after: float | None = None,
    ) -> None:  # fmt: skip
        super().__init__(f"telegram {method}: {description}")
        self.method = method
        self.description = description
        self.code = code
        self.retry_after = retry_after


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
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._token = token
        self._base = f"{base_url.rstrip('/')}/bot{token}"
        self._client = httpx.AsyncClient(transport=transport, timeout=30.0)
        self._sleep = sleep
        _mask_httpx_logs(token)

    def __repr__(self) -> str:
        return "TelegramApi(<token hidden>)"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        files: dict[str, Upload] | None = None,
        timeout: float = 30.0,
        retry_timeouts: bool = True,
    ) -> Any:
        """One Bot API method; returns `result`. Raises TelegramError."""
        params = {key: value for key, value in (params or {}).items() if value is not None}
        attempt = 0
        while True:
            try:
                if files:
                    form = {
                        key: value if isinstance(value, str) else json.dumps(value)
                        for key, value in params.items()
                    }
                    response = await self._client.post(
                        f"{self._base}/{method}", data=form, files=files, timeout=timeout
                    )
                else:
                    response = await self._client.post(
                        f"{self._base}/{method}", json=params, timeout=timeout
                    )
            except httpx.TimeoutException as exc:
                if not retry_timeouts or attempt >= len(RETRY_DELAYS_S):
                    raise TelegramError(method, "timed out") from exc
            except httpx.TransportError as exc:
                if attempt >= len(RETRY_DELAYS_S):
                    raise TelegramError(method, f"network error ({type(exc).__name__})") from None
            else:
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
    ) -> dict[str, Any]:
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
    ) -> dict[str, Any]:
        """A photo or video answering an earlier message, without a sound."""
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
            },
            files={"file": upload},
            timeout=timeout,
            retry_timeouts=False,
        )
        return result

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
