"""Camera inventory + per-camera diagnostics (M1).

`GET /api/v1/cameras` joins the configured cameras with live recorder state and gateway
stream readiness; `GET /api/v1/cameras/{camera_id}` adds the adapter's `probe()`
diagnostics. Read-only; guarded by the `read:streams` scope.
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from vidette.adapters.base import AdapterError, CameraAdapter, available_adapters
from vidette.api.errors import problem
from vidette.auth.deps import require_scope
from vidette.core.config import CameraConfig
from vidette.runtime import AppRuntime

router = APIRouter(
    prefix="/api/v1/cameras",
    tags=["cameras"],
    dependencies=[Depends(require_scope("read:streams"))],
)


class CameraSummary(BaseModel):
    """One row of the camera list; the web client renders this verbatim."""

    id: str
    name: str
    adapter: str
    record_mode: str
    state: str
    last_segment_at: float | None
    last_error: str | None  # recorder diagnosis, shown verbatim when live view degrades
    stream_ready: bool


class ProbeInfo(BaseModel):
    status: str
    detail: str


class CameraDetail(CameraSummary):
    probe: ProbeInfo


def _runtime(request: Request) -> AppRuntime:
    return cast(AppRuntime, request.app.state.runtime)


def _summary(
    camera_id: str,
    camera: CameraConfig,
    runtime: AppRuntime,
    ready_streams: frozenset[str],
) -> CameraSummary:
    recorder_status = runtime.recorder.status().get(camera_id)
    return CameraSummary(
        id=camera_id,
        name=camera.name or camera_id,
        adapter=camera.adapter,
        record_mode=camera.record.mode.value,
        state=recorder_status.state if recorder_status is not None else "idle",
        last_segment_at=recorder_status.last_segment_at if recorder_status is not None else None,
        last_error=recorder_status.last_error if recorder_status is not None else None,
        stream_ready=camera_id in ready_streams,
    )


async def _probe(camera_id: str, camera: CameraConfig) -> ProbeInfo:
    try:
        registry = available_adapters()
    except AdapterError as exc:
        return ProbeInfo(
            status="misconfigured",
            detail=f"{exc} — remove or repair the broken adapter plugin, then retry",
        )
    adapter: CameraAdapter | None = registry.get(camera.adapter)
    if adapter is None:
        installed = ", ".join(sorted(registry))
        return ProbeInfo(
            status="misconfigured",
            detail=(
                f"adapter '{camera.adapter}' is not installed — set "
                f"cameras.{camera_id}.adapter to one of: {installed}, or install the plugin"
            ),
        )
    try:
        result = await adapter.probe(camera_id, camera)
    except AdapterError as exc:
        return ProbeInfo(status="misconfigured", detail=str(exc))
    return ProbeInfo(status=result.status.value, detail=result.detail)


@router.get("")
async def list_cameras(request: Request) -> list[CameraSummary]:
    runtime = _runtime(request)
    ready = (await runtime.go2rtc.health()).streams
    return [
        _summary(camera_id, camera, runtime, ready)
        for camera_id, camera in runtime.config.cameras.items()
    ]


@router.get("/{camera_id}")
async def get_camera(camera_id: str, request: Request) -> CameraDetail:
    runtime = _runtime(request)
    camera = runtime.config.cameras.get(camera_id)
    if camera is None:
        raise problem(
            404,
            "Camera not found",
            f"no camera '{camera_id}' is configured — list configured ids via "
            "GET /api/v1/cameras, or add it under cameras: in the config",
        )
    ready = (await runtime.go2rtc.health()).streams
    summary = _summary(camera_id, camera, runtime, ready)
    return CameraDetail(**summary.model_dump(), probe=await _probe(camera_id, camera))
