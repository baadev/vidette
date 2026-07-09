"""UI-managed cameras: CRUD under /api/v1/config/cameras, plus discovery and probing.

The hand-written YAML stays the IaC source of truth and is never rewritten here. Cameras
created through this API live in the `managed_cameras` table, validate against the same
schema as the file (`core.managed.validate_camera_payload`) and are merged into the
effective config by the runtime (`reload_cameras()` re-merges + hot-applies). File-defined
cameras are listed but read-only in this API — edit /config/vidette.yaml instead.

Also here so the UI can offer "find my cameras" and "test connection" before saving:
ONVIF WS-Discovery (`POST /discover`) and per-camera adapter probing (`POST /{id}/probe`).

Reads require the `read:config` scope, writes `write:config`.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from vidette.adapters import onvif
from vidette.adapters.base import AdapterError, CameraAdapter, available_adapters
from vidette.api.errors import problem
from vidette.auth.deps import require_scope
from vidette.core.config import CameraConfig, VidetteConfig
from vidette.core.managed import validate_camera_payload
from vidette.db import Database

router = APIRouter(prefix="/api/v1/config/cameras", tags=["config"])

_REQUIRE_READ = Depends(require_scope("read:config"))
_REQUIRE_WRITE = Depends(require_scope("write:config"))


class ManagedCamerasRuntime(Protocol):
    """The slice of AppRuntime this router codes against.

    - `config.cameras` is the merged *effective* set (file + managed rows, file wins);
    - `file_cameras` is exactly what the YAML file defines;
    - `reload_cameras()` re-merges the stored rows into the effective config and
      hot-applies it, returning the merge warnings.
    """

    config: VidetteConfig
    file_cameras: dict[str, CameraConfig]
    db: Database

    async def reload_cameras(self) -> list[str]: ...


class CameraOut(BaseModel):
    """One camera as the config UI sees it; `config` is the full CameraConfig JSON dump."""

    id: str
    source: Literal["file", "managed"]
    editable: bool
    config: dict[str, Any]


class CameraWriteOut(CameraOut):
    warnings: list[str]  # merge/apply warnings from reload_cameras(), shown after saving


class CameraCreateIn(BaseModel):
    id: str
    config: dict[str, Any] = Field(default_factory=dict)


class CameraUpdateIn(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)


class DiscoveredDeviceOut(BaseModel):
    address: str  # WS-Addressing endpoint reference (urn:uuid:...)
    xaddr: str  # device-service URL to point the onvif adapter at
    scopes: list[str]


class DiscoveryOut(BaseModel):
    devices: list[DiscoveredDeviceOut]


class ProbeOut(BaseModel):
    status: str
    detail: str


def _runtime(request: Request) -> ManagedCamerasRuntime:
    return cast(ManagedCamerasRuntime, request.app.state.runtime)


def _file_defined(camera_id: str) -> str:
    return (
        f"camera '{camera_id}' is defined in the config file — edit /config/vidette.yaml "
        "instead (the file is the source of truth; the UI never rewrites it)"
    )


def _camera_out(
    camera_id: str, camera: CameraConfig, source: Literal["file", "managed"]
) -> CameraOut:
    return CameraOut(
        id=camera_id,
        source=source,
        editable=source == "managed",
        config=camera.model_dump(mode="json"),
    )


def _validated_or_422(camera_id: str, payload: dict[str, Any]) -> CameraConfig:
    try:
        return validate_camera_payload(camera_id, payload)
    except ValueError as exc:
        # The message already names the offending keys and what to change — verbatim.
        raise problem(422, "Invalid camera config", str(exc)) from exc


@router.get("", dependencies=[_REQUIRE_READ])
async def list_config_cameras(request: Request) -> list[CameraOut]:
    """Every effective camera, marked by where it is defined and whether the UI may edit it."""
    runtime = _runtime(request)
    return [
        _camera_out(camera_id, camera, "file" if camera_id in runtime.file_cameras else "managed")
        for camera_id, camera in sorted(runtime.config.cameras.items())
    ]


@router.post("", status_code=201, dependencies=[_REQUIRE_WRITE])
async def create_config_camera(body: CameraCreateIn, request: Request) -> CameraWriteOut:
    runtime = _runtime(request)
    if body.id in runtime.file_cameras:
        raise problem(409, "Camera already exists", _file_defined(body.id))
    stored = {row.id for row in await runtime.db.list_managed_cameras()}
    if body.id in stored or body.id in runtime.config.cameras:
        raise problem(
            409,
            "Camera already exists",
            f"camera '{body.id}' already exists — update it via "
            f"PUT /api/v1/config/cameras/{body.id}, or pick another id",
        )
    camera = _validated_or_422(body.id, body.config)
    # Store the raw just-validated payload, not model_dump(mode="json"): pydantic dumps
    # durations as ISO-8601 ("P3D"), which the config schema's Duration parser refuses —
    # the payload as sent is the one representation guaranteed to re-validate at merge time.
    await runtime.db.upsert_managed_camera(body.id, body.config)
    warnings = await runtime.reload_cameras()
    return CameraWriteOut(
        id=body.id,
        source="managed",
        editable=True,
        config=camera.model_dump(mode="json"),
        warnings=warnings,
    )


@router.put("/{camera_id}", dependencies=[_REQUIRE_WRITE])
async def update_config_camera(
    camera_id: str, body: CameraUpdateIn, request: Request
) -> CameraWriteOut:
    runtime = _runtime(request)
    if camera_id in runtime.file_cameras:
        raise problem(409, "Camera is read-only", _file_defined(camera_id))
    stored = {row.id for row in await runtime.db.list_managed_cameras()}
    if camera_id not in stored:
        raise problem(
            404,
            "Camera not found",
            f"no UI-managed camera '{camera_id}' — list cameras via "
            "GET /api/v1/config/cameras, or create it via POST",
        )
    camera = _validated_or_422(camera_id, body.config)
    await runtime.db.upsert_managed_camera(camera_id, body.config)  # raw payload, see POST
    warnings = await runtime.reload_cameras()
    return CameraWriteOut(
        id=camera_id,
        source="managed",
        editable=True,
        config=camera.model_dump(mode="json"),
        warnings=warnings,
    )


@router.delete("/{camera_id}", status_code=204, dependencies=[_REQUIRE_WRITE])
async def delete_config_camera(camera_id: str, request: Request) -> None:
    """Delete a UI-managed camera; file-defined cameras are read-only here.

    Delete-first on purpose: when an id collides (defined in both the file and the UI)
    the merge keeps the file camera and warns — deleting here removes the shadowed UI
    copy, which is exactly what that warning tells the user to do.
    """
    runtime = _runtime(request)
    if not await runtime.db.delete_managed_camera(camera_id):
        if camera_id in runtime.file_cameras:
            raise problem(409, "Camera is read-only", _file_defined(camera_id))
        raise problem(
            404,
            "Camera not found",
            f"no UI-managed camera '{camera_id}' — list cameras via GET /api/v1/config/cameras",
        )
    await runtime.reload_cameras()


@router.post("/discover", dependencies=[_REQUIRE_WRITE])
async def discover_onvif_devices() -> DiscoveryOut:
    """Best-effort ONVIF WS-Discovery sweep of the local network (~3 s, may be empty)."""
    devices = await onvif.discover(timeout_s=3.0)
    return DiscoveryOut(
        devices=[
            DiscoveredDeviceOut(address=device.address, xaddr=device.xaddr, scopes=device.scopes)
            for device in devices
        ]
    )


@router.post("/{camera_id}/probe", dependencies=[_REQUIRE_READ])
async def probe_config_camera(camera_id: str, request: Request) -> ProbeOut:
    """Run the camera's adapter probe — 'test connection' with an actionable diagnosis."""
    runtime = _runtime(request)
    camera = runtime.config.cameras.get(camera_id)
    if camera is None:
        raise problem(
            404,
            "Camera not found",
            f"no camera '{camera_id}' is configured — list configured ids via "
            "GET /api/v1/config/cameras",
        )
    try:
        registry = available_adapters()
    except AdapterError as exc:
        return ProbeOut(
            status="misconfigured",
            detail=f"{exc} — remove or repair the broken adapter plugin, then retry",
        )
    adapter: CameraAdapter | None = registry.get(camera.adapter)
    if adapter is None:
        installed = ", ".join(sorted(registry))
        raise problem(
            422,
            "Unknown adapter",
            f"adapter '{camera.adapter}' is not installed — available adapters: "
            f"{installed}; set the camera's adapter to one of them or install the plugin",
        )
    try:
        result = await adapter.probe(camera_id, camera)
    except AdapterError as exc:
        return ProbeOut(status="misconfigured", detail=str(exc))
    return ProbeOut(status=result.status.value, detail=result.detail)
