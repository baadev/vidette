"""Live stream access (M1): WHEP signaling + JPEG snapshots, proxied through go2rtc.

The go2rtc admin API is never exposed to browsers (ADR-0002) — the browser talks to these
authenticated endpoints and Vidette relays to the gateway. Guarded by the `read:streams`
scope. Camera ids are validated against the config before any URL is built.
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from vidette.api.errors import problem
from vidette.auth.deps import require_scope
from vidette.runtime import AppRuntime
from vidette.streams.go2rtc import GatewayError

router = APIRouter(
    prefix="/api/v1/streams",
    tags=["streams"],
    dependencies=[Depends(require_scope("read:streams"))],
)


class StreamInfo(BaseModel):
    webrtc: str
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
        snapshot=f"/api/v1/streams/{camera}/snapshot.jpeg",
    )


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
