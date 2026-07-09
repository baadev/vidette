"""Live stream access: WHEP signaling, MSE relay, JPEG snapshots — proxied through go2rtc.

The go2rtc admin API is never exposed to browsers (ADR-0002) — the browser talks to these
authenticated endpoints and Vidette relays to the gateway. Guarded by the `read:streams`
scope. Camera ids are validated against the config before any URL is built.

The MSE WebSocket (`/{camera}/mse`) exists because WebRTC needs reachable ICE candidates,
and a containerized gateway only knows its bridge/STUN addresses (field case: the SDP
answer advertised the public IP with ephemeral ports — dead on arrival for a LAN browser).
MSE is plain WebSocket + fMP4: same origin, same cookie, works in every topology. It lives
on a separate router without the HTTP scope dependency — WS auth must complete *before*
`accept()` to send a proper close code, so it runs `ws_principal` inline like /api/v1/ws.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import cast

import websockets
from fastapi import APIRouter, Depends, Request, Response, WebSocket
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from starlette.websockets import WebSocketDisconnect

from vidette.api.errors import problem
from vidette.auth.deps import require_scope, ws_principal
from vidette.runtime import AppRuntime
from vidette.streams.go2rtc import GatewayError

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/streams",
    tags=["streams"],
    dependencies=[Depends(require_scope("read:streams"))],
)

# WebSocket routes cannot share the HTTP dependency above (it needs a Request); auth is
# enforced inline. Both routers are included by api/app.py.
ws_router = APIRouter(prefix="/api/v1/streams", tags=["streams"])

CLOSE_UNAUTHENTICATED = 4401
CLOSE_NOT_FOUND = 4404
CLOSE_GATEWAY_ERROR = 4502


class StreamInfo(BaseModel):
    webrtc: str
    mse: str
    snapshot: str


def _runtime(request: Request) -> AppRuntime:
    return cast(AppRuntime, request.app.state.runtime)


def _ensure_camera(runtime: AppRuntime, camera: str) -> None:
    if camera not in runtime.config.cameras:
        raise problem(
            404,
            "Camera not found",
            f"no camera '{camera}' is configured — list configured ids via GET /api/v1/cameras",
        )


@router.get("/{camera}")
async def stream_info(camera: str, request: Request) -> StreamInfo:
    _ensure_camera(_runtime(request), camera)
    return StreamInfo(
        webrtc=f"/api/v1/streams/{camera}/whep",
        mse=f"/api/v1/streams/{camera}/mse",
        snapshot=f"/api/v1/streams/{camera}/snapshot.jpeg",
    )


@ws_router.websocket("/{camera}/mse")
async def mse(camera: str, websocket: WebSocket) -> None:
    """Authenticated relay to go2rtc's MSE WebSocket (JSON control + fMP4 binary frames)."""
    runtime = cast(AppRuntime, websocket.app.state.runtime)
    principal = await ws_principal(websocket)
    if principal is None or not principal.allows("read:streams"):
        await websocket.close(
            code=CLOSE_UNAUTHENTICATED,
            reason="log in for a session cookie, or connect with Authorization: Bearer vd_…",
        )
        return
    try:
        upstream_url = runtime.go2rtc.mse_ws_url(camera)
    except GatewayError:
        await websocket.close(
            code=CLOSE_NOT_FOUND,
            reason=f"no camera '{camera}' is configured",
        )
        return

    await websocket.accept()
    try:
        async with websockets.connect(upstream_url, max_size=None, open_timeout=5) as upstream:

            async def pump_to_gateway() -> None:
                while True:
                    message = await websocket.receive()
                    if message.get("type") == "websocket.disconnect":
                        return
                    text = message.get("text")
                    if text is not None:
                        await upstream.send(text)
                        continue
                    data = message.get("bytes")
                    if data is not None:
                        await upstream.send(data)

            async def pump_to_browser() -> None:
                async for frame in upstream:
                    if isinstance(frame, str):
                        await websocket.send_text(frame)
                    else:
                        await websocket.send_bytes(bytes(frame))

            tasks = {
                asyncio.create_task(pump_to_gateway(), name=f"mse-up-{camera}"),
                asyncio.create_task(pump_to_browser(), name=f"mse-down-{camera}"),
            }
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(
                    exc, WebSocketDisconnect | websockets.exceptions.ConnectionClosed
                ):
                    logger.warning("mse relay for %s ended with %r", camera, exc)
    except (OSError, websockets.exceptions.WebSocketException) as exc:
        logger.warning("mse relay for %s: gateway unreachable: %s", camera, exc)
        with contextlib.suppress(Exception):
            await websocket.close(
                code=CLOSE_GATEWAY_ERROR,
                reason="stream gateway unreachable — check that go2rtc is running",
            )
        return
    with contextlib.suppress(Exception):
        await websocket.close()


@router.post("/{camera}/whep")
async def whep(camera: str, request: Request) -> PlainTextResponse:
    runtime = _runtime(request)
    _ensure_camera(runtime, camera)
    body = await request.body()
    try:
        offer = body.decode("utf-8")
    except UnicodeDecodeError:
        raise problem(
            422,
            "Invalid SDP offer",
            "the request body must be a UTF-8 SDP offer sent as-is "
            "(Content-Type: application/sdp)",
        ) from None
    if not offer.strip():
        raise problem(
            422,
            "Missing SDP offer",
            "send the WHEP SDP offer as the raw request body "
            "(Content-Type: application/sdp)",
        )
    try:
        answer = await runtime.go2rtc.whep_exchange(camera, offer)
    except GatewayError as exc:
        raise problem(502, "Stream gateway error", str(exc)) from exc
    return PlainTextResponse(answer, media_type="application/sdp")


@router.get("/{camera}/snapshot.jpeg")
async def snapshot(camera: str, request: Request) -> Response:
    runtime = _runtime(request)
    _ensure_camera(runtime, camera)
    try:
        frame = await runtime.go2rtc.snapshot(camera)
    except GatewayError as exc:
        raise problem(502, "Stream gateway error", str(exc)) from exc
    return Response(content=frame, media_type="image/jpeg")
