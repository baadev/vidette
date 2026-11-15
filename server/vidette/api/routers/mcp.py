"""The assistant's tools over MCP (ADR-0017, docs/assistant.md#mcp).

`POST /api/v1/mcp` speaks the Model Context Protocol's streamable HTTP transport in its
simplest form: one JSON-RPC request per POST, one JSON answer, no sessions and no event
stream (GET answers 405). Behind the same auth as the rest of the API (a session or an
API token with `read:events`), on the server's own address (127.0.0.1 by default).

Read-only: find events, an event's details, the clips of every camera around a moment,
where an identity was seen, today's digest. Videos are links to the API
(`…/clip.mp4`, `…/linked.mp4`), never bytes in the answer. An MCP client may hand
everything to a cloud model, so no name is ever in a result (identity numbers and
"свой" only), and no face crop, embedding or frame.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Mapping
from typing import Any, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from vidette import __version__
from vidette.assistant.tools import READ_ONLY, ToolError, Tools
from vidette.auth.deps import require_scope
from vidette.runtime import AppRuntime
from vidette.telegram import text as say

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1",
    tags=["mcp"],
    dependencies=[Depends(require_scope("read:events"))],
)

PROTOCOL = "2025-06-18"
INSTRUCTIONS = (
    "Vidette watches a home's cameras. Use find_events for what happened when, "
    "event_detail for one event, clips_at for what every camera recorded around a moment "
    "(battery cameras record only while awake: say so when one has nothing), "
    "identity_sightings for where one face was seen (numbers, never names). Two clips show "
    "the same person only when same_person_by_face says so. Times are local."
)


def _tools(runtime: AppRuntime) -> Tools:
    telegram = runtime.telegram
    names = runtime.config.notifications.telegram
    camera_names = names.camera_names if names is not None else {}

    async def digest() -> str:
        assert telegram is not None
        day = await telegram.day(telegram.tg.recipient_chats[0])
        text = say.day_digest_text(day, now=time.time(), evening=False)
        return re.sub(r"<[^>]+>", "", text)

    return Tools(
        runtime.config, runtime.db, identities=runtime.identities,
        camera_name=lambda camera: camera_names.get(camera, camera), names=False,
        digest=digest if telegram is not None else None,
    )  # fmt: skip


def _specs(tools: Tools, *, with_today: bool) -> list[dict[str, Any]]:
    out = []
    for spec in tools.specs(read_only=True):
        if spec["name"] == "today" and not with_today:
            continue
        out.append(
            {
                "name": spec["name"],
                "description": spec["description"],
                "inputSchema": spec["parameters"],
                "annotations": {"readOnlyHint": True},
            }
        )
    return out


def _ok(request_id: Any, result: dict[str, Any]) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    )


def _with_links(data: dict[str, Any]) -> dict[str, Any]:
    """Event ids in a result get the API paths of their clip and mosaic."""
    for clip in data.get("clips") or []:
        if isinstance(clip, dict) and clip.get("event_id"):
            clip["clip_url"] = f"/api/v1/events/{clip['event_id']}/clip.mp4"
    if isinstance(data.get("id"), str):
        data["clip_url"] = f"/api/v1/events/{data['id']}/clip.mp4"
        data["all_cameras_url"] = f"/api/v1/events/{data['id']}/linked.mp4"
    return data


@router.get("/mcp")
async def mcp_get() -> Response:
    return Response(status_code=405, headers={"allow": "POST"})


@router.post("/mcp", response_model=None)
async def mcp(request: Request) -> Response:
    runtime = cast(AppRuntime, request.app.state.runtime)
    try:
        message = json.loads(await request.body())
    except ValueError:
        return _error(None, -32700, "not JSON")
    if not isinstance(message, Mapping) or message.get("jsonrpc") != "2.0":
        return _error(None, -32600, "one JSON-RPC 2.0 request per POST")
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:  # a notification (e.g. notifications/initialized)
        return Response(status_code=202)
    params = message.get("params") if isinstance(message.get("params"), Mapping) else {}
    tools = _tools(runtime)
    with_today = runtime.telegram is not None
    if method == "initialize":
        return _ok(
            request_id,
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "vidette", "version": __version__},
                "instructions": INSTRUCTIONS,
            },
        )
    if method == "ping":
        return _ok(request_id, {})
    if method == "tools/list":
        return _ok(request_id, {"tools": _specs(tools, with_today=with_today)})
    if method == "tools/call":
        assert isinstance(params, Mapping)
        name = params.get("name")
        args = params.get("arguments") if isinstance(params.get("arguments"), Mapping) else {}
        allowed = {s["name"] for s in _specs(tools, with_today=with_today)}
        if name not in allowed or name not in READ_ONLY:
            return _error(request_id, -32602, f"unknown tool {name!r}")
        try:
            result = await tools.call(str(name), cast(Mapping[str, Any], args))
        except ToolError as exc:
            failed = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            return _ok(request_id, failed)
        data = _with_links(dict(result.data))
        text = json.dumps(data, ensure_ascii=False)
        answer = {"content": [{"type": "text", "text": text}], "structuredContent": data}
        return _ok(request_id, {**answer, "isError": False})
    return _error(request_id, -32601, f"method {method!r} is not supported")
