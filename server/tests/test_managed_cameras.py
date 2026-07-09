"""UI-managed cameras: merge semantics, payload validation, /api/v1/config/cameras API.

The app under test is a bare FastAPI() with only the config-cameras router mounted and
`app.state.runtime` a SimpleNamespace implementing the router's runtime contract: a real
`Database` on tmp_path (managed_cameras is implemented there), a static merged config +
`file_cameras`, and a `reload_cameras` spy that records calls and returns canned warnings.
Auth is overridden at `current_principal`, per the deps.py wiring contract. Requests run
through httpx's ASGITransport so DB calls, requests and assertions share one event loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import vidette.auth.deps as auth_deps
from vidette.adapters import onvif
from vidette.api.routers.config_cameras import router as config_cameras_router
from vidette.auth.service import ANONYMOUS_ADMIN, Principal
from vidette.core.config import CameraConfig, VidetteConfig
from vidette.core.managed import CAMERA_ID_RE, merge_managed_cameras, validate_camera_payload
from vidette.db import Database, ManagedCameraRow

FILE_CAM: dict[str, Any] = {
    "adapter": "rtsp",
    "name": "From File",
    "source": {"main": "rtsp://user:pw@203.0.113.10:554/stream1"},
}
UI_CAM: dict[str, Any] = {
    "adapter": "rtsp",
    "source": {"main": "rtsp://user:pw@203.0.113.20:554/stream1"},
}
BASE = "/api/v1/config/cameras"

COLLISION_WARNING = (
    "cameras.front-door: defined in both the config file and the UI — the file wins; "
    "delete the UI copy to silence this"
)


def _file_config() -> VidetteConfig:
    return VidetteConfig.model_validate({"cameras": {"front-door": FILE_CAM}})


def _row(camera_id: str, config: dict[str, Any]) -> ManagedCameraRow:
    return ManagedCameraRow(id=camera_id, config=config, created_at=1.0, updated_at=1.0)


# --- merge -------------------------------------------------------------------------------------


def test_merge_adds_managed_cameras_without_touching_the_input() -> None:
    config = _file_config()
    merged, warnings = merge_managed_cameras(config, [_row("garage", UI_CAM)])
    assert warnings == []
    assert sorted(merged.cameras) == ["front-door", "garage"]
    garage = merged.cameras["garage"]
    assert garage.source is not None
    assert garage.source.main == UI_CAM["source"]["main"]
    assert sorted(config.cameras) == ["front-door"]  # input config is not mutated


def test_merge_collision_file_wins_and_is_reported() -> None:
    ui_copy = {**UI_CAM, "name": "From UI"}
    merged, warnings = merge_managed_cameras(_file_config(), [_row("front-door", ui_copy)])
    assert merged.cameras["front-door"].name == "From File"  # the file wins
    assert warnings == [COLLISION_WARNING]


def test_merge_skips_invalid_row_with_warning_and_keeps_the_rest() -> None:
    rows = [_row("bad-cam", {"detect": {"fps": -3}}), _row("garage", UI_CAM)]
    merged, warnings = merge_managed_cameras(_file_config(), rows)
    assert "bad-cam" not in merged.cameras
    assert "garage" in merged.cameras  # one bad row must not take the others down
    assert len(warnings) == 1
    assert "cameras.bad-cam" in warnings[0]
    assert "detect.fps" in warnings[0]  # the warning names the actual error


def test_merge_skips_row_whose_id_is_invalid() -> None:
    merged, warnings = merge_managed_cameras(_file_config(), [_row("Bad_ID", UI_CAM)])
    assert "Bad_ID" not in merged.cameras
    assert len(warnings) == 1
    assert "Bad_ID" in warnings[0]


# --- validate_camera_payload ---------------------------------------------------------------------


def test_camera_id_regex() -> None:
    assert CAMERA_ID_RE.match("front-door")
    assert CAMERA_ID_RE.match("cam2")
    for bad in ("", "-front", "Front", "front_door", "front door"):
        assert CAMERA_ID_RE.match(bad) is None, bad


def test_validate_camera_payload_fills_defaults() -> None:
    camera = validate_camera_payload("garage", UI_CAM)
    assert isinstance(camera, CameraConfig)
    assert camera.detect.fps == 5.0  # schema defaults apply


def test_validate_camera_payload_rejects_bad_id_readably() -> None:
    with pytest.raises(ValueError, match=r"camera id 'Front Door' must match"):
        validate_camera_payload("Front Door", UI_CAM)


def test_validate_camera_payload_bad_zone_points_is_readable() -> None:
    payload = {"zones": {"door": {"kind": "entry", "points": [[0.1, 0.2], [1.5, 0.5], [0.3, 0.9]]}}}
    with pytest.raises(ValueError) as excinfo:
        validate_camera_payload("garage", payload)
    message = str(excinfo.value)
    assert "zones.door" in message  # pydantic loc joined into a dotted path
    assert "normalized to 0.0–1.0" in message


def test_validate_camera_payload_joins_multiple_errors() -> None:
    with pytest.raises(ValueError) as excinfo:
        validate_camera_payload("garage", {"detect": {"fps": -3}, "bogus": True})
    message = str(excinfo.value)
    assert "detect.fps" in message
    assert "bogus" in message
    assert "; " in message  # one readable line, not a pydantic dump


# --- router harness ------------------------------------------------------------------------------


class ReloadSpy:
    """Stands in for AppRuntime.reload_cameras: records calls, returns canned warnings."""

    def __init__(self) -> None:
        self.calls = 0
        self.warnings: list[str] = []

    async def __call__(self) -> list[str]:
        self.calls += 1
        return list(self.warnings)


@dataclass
class Harness:
    client: AsyncClient
    app: FastAPI
    runtime: SimpleNamespace
    db: Database
    reload_spy: ReloadSpy

    def add_effective_camera(self, camera_id: str, config: dict[str, Any]) -> None:
        """Put a camera into the merged effective config only (as a re-merge would)."""
        camera = CameraConfig.model_validate(config)
        self.runtime.config = self.runtime.config.model_copy(
            update={"cameras": {**self.runtime.config.cameras, camera_id: camera}}
        )


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    database = Database(tmp_path / "vidette.db")
    await database.connect()
    config = _file_config()
    reload_spy = ReloadSpy()
    runtime = SimpleNamespace(
        config=config,
        file_cameras=dict(config.cameras),
        db=database,
        reload_cameras=reload_spy,
    )
    app = FastAPI()
    app.include_router(config_cameras_router)
    app.state.runtime = runtime
    app.dependency_overrides[auth_deps.current_principal] = lambda: ANONYMOUS_ADMIN
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    try:
        yield Harness(client=client, app=app, runtime=runtime, db=database, reload_spy=reload_spy)
    finally:
        await client.aclose()
        await database.close()


def _problem(body: dict[str, Any]) -> dict[str, Any]:
    detail = body["detail"]
    assert detail["type"] == "about:blank"
    assert isinstance(detail["title"], str) and detail["title"]
    assert isinstance(detail["detail"], str) and detail["detail"]
    return detail  # type: ignore[no-any-return]


# --- GET ----------------------------------------------------------------------------------------


async def test_list_marks_source_and_editability_sorted_by_id(harness: Harness) -> None:
    harness.add_effective_camera("garage", UI_CAM)
    response = await harness.client.get(BASE)
    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body] == ["front-door", "garage"]  # sorted by id
    front, garage = body
    assert front["source"] == "file"
    assert front["editable"] is False
    assert front["config"]["name"] == "From File"
    assert garage["source"] == "managed"
    assert garage["editable"] is True
    assert garage["config"]["record"]["mode"] == "continuous"  # full CameraConfig dump


# --- POST ---------------------------------------------------------------------------------------


async def test_create_persists_row_reloads_and_echoes_camera(harness: Harness) -> None:
    response = await harness.client.post(BASE, json={"id": "garage", "config": UI_CAM})
    assert response.status_code == 201
    body = response.json()
    assert body["id"] == "garage"
    assert body["source"] == "managed"
    assert body["editable"] is True
    assert body["warnings"] == []
    assert body["config"]["source"]["main"] == UI_CAM["source"]["main"]
    assert body["config"]["detect"]["fps"] == 5.0  # normalized: defaults filled in
    assert harness.reload_spy.calls == 1
    rows = await harness.db.list_managed_cameras()
    assert [row.id for row in rows] == ["garage"]
    assert rows[0].config == UI_CAM  # the raw payload is stored — it re-validates at merge


async def test_create_surfaces_reload_warnings(harness: Harness) -> None:
    harness.reload_spy.warnings = ["cameras.garage: something worth knowing"]
    response = await harness.client.post(BASE, json={"id": "garage", "config": UI_CAM})
    assert response.status_code == 201
    assert response.json()["warnings"] == ["cameras.garage: something worth knowing"]


async def test_create_collision_with_file_camera_409(harness: Harness) -> None:
    response = await harness.client.post(BASE, json={"id": "front-door", "config": UI_CAM})
    assert response.status_code == 409
    detail = _problem(response.json())
    assert "defined in the config file — edit /config/vidette.yaml instead" in detail["detail"]
    assert await harness.db.list_managed_cameras() == []
    assert harness.reload_spy.calls == 0


async def test_create_duplicate_managed_camera_409(harness: Harness) -> None:
    first = await harness.client.post(BASE, json={"id": "garage", "config": UI_CAM})
    assert first.status_code == 201
    response = await harness.client.post(BASE, json={"id": "garage", "config": UI_CAM})
    assert response.status_code == 409
    detail = _problem(response.json())
    assert "PUT /api/v1/config/cameras/garage" in detail["detail"]  # points at the fix
    assert harness.reload_spy.calls == 1  # no reload for the rejected write


async def test_create_invalid_config_422_with_message_verbatim(harness: Harness) -> None:
    response = await harness.client.post(
        BASE, json={"id": "garage", "config": {"detect": {"fps": -3}}}
    )
    assert response.status_code == 422
    detail = _problem(response.json())
    assert detail["detail"] == "detect.fps: Input should be greater than 0"
    assert await harness.db.list_managed_cameras() == []
    assert harness.reload_spy.calls == 0


async def test_create_invalid_id_422(harness: Harness) -> None:
    response = await harness.client.post(BASE, json={"id": "Bad_ID", "config": {}})
    assert response.status_code == 422
    assert "camera id 'Bad_ID'" in _problem(response.json())["detail"]


# --- PUT ----------------------------------------------------------------------------------------


async def test_update_managed_camera(harness: Harness) -> None:
    await harness.client.post(BASE, json={"id": "garage", "config": UI_CAM})
    updated = {**UI_CAM, "name": "Garage West"}
    response = await harness.client.put(f"{BASE}/garage", json={"config": updated})
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "garage"
    assert body["source"] == "managed"
    assert body["editable"] is True
    assert body["config"]["name"] == "Garage West"
    assert body["warnings"] == []
    rows = await harness.db.list_managed_cameras()
    assert rows[0].config["name"] == "Garage West"
    assert harness.reload_spy.calls == 2  # create + update


async def test_update_file_camera_409(harness: Harness) -> None:
    response = await harness.client.put(f"{BASE}/front-door", json={"config": UI_CAM})
    assert response.status_code == 409
    detail = _problem(response.json())
    assert "defined in the config file — edit /config/vidette.yaml instead" in detail["detail"]


async def test_update_unknown_camera_404(harness: Harness) -> None:
    response = await harness.client.put(f"{BASE}/ghost", json={"config": UI_CAM})
    assert response.status_code == 404
    assert "GET /api/v1/config/cameras" in _problem(response.json())["detail"]


async def test_update_invalid_config_422_keeps_stored_row(harness: Harness) -> None:
    await harness.client.post(BASE, json={"id": "garage", "config": UI_CAM})
    response = await harness.client.put(
        f"{BASE}/garage", json={"config": {"zones": {"door": {"kind": "entry", "points": []}}}}
    )
    assert response.status_code == 422
    assert "zones.door" in _problem(response.json())["detail"]
    rows = await harness.db.list_managed_cameras()
    assert rows[0].config == UI_CAM  # rejected write left the row alone
    assert harness.reload_spy.calls == 1


# --- DELETE -------------------------------------------------------------------------------------


async def test_delete_managed_camera_204(harness: Harness) -> None:
    await harness.client.post(BASE, json={"id": "garage", "config": UI_CAM})
    response = await harness.client.delete(f"{BASE}/garage")
    assert response.status_code == 204
    assert response.content == b""
    assert await harness.db.list_managed_cameras() == []
    assert harness.reload_spy.calls == 2  # create + delete


async def test_delete_file_camera_409(harness: Harness) -> None:
    response = await harness.client.delete(f"{BASE}/front-door")
    assert response.status_code == 409
    detail = _problem(response.json())
    assert "defined in the config file — edit /config/vidette.yaml instead" in detail["detail"]
    assert harness.reload_spy.calls == 0


async def test_delete_unknown_camera_404(harness: Harness) -> None:
    response = await harness.client.delete(f"{BASE}/ghost")
    assert response.status_code == 404
    assert harness.reload_spy.calls == 0


async def test_delete_removes_shadowed_ui_copy_on_collision(harness: Harness) -> None:
    # The collision warning says "delete the UI copy to silence this" — this is that path.
    await harness.db.upsert_managed_camera("front-door", UI_CAM)
    response = await harness.client.delete(f"{BASE}/front-door")
    assert response.status_code == 204
    assert await harness.db.list_managed_cameras() == []
    assert harness.reload_spy.calls == 1


# --- discover -----------------------------------------------------------------------------------


async def test_discover_returns_devices(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[float] = []

    async def fake_discover(timeout_s: float = 3.0) -> list[onvif.DiscoveredDevice]:
        calls.append(timeout_s)
        return [
            onvif.DiscoveredDevice(
                xaddr="http://192.0.2.7/onvif/device_service",
                scopes=["onvif://www.onvif.org/name/Porch"],
                address="urn:uuid:porch-1",
            )
        ]

    monkeypatch.setattr(onvif, "discover", fake_discover)
    response = await harness.client.post(f"{BASE}/discover")
    assert response.status_code == 200
    assert response.json() == {
        "devices": [
            {
                "address": "urn:uuid:porch-1",
                "xaddr": "http://192.0.2.7/onvif/device_service",
                "scopes": ["onvif://www.onvif.org/name/Porch"],
            }
        ]
    }
    assert calls == [3.0]


# --- probe --------------------------------------------------------------------------------------


async def test_probe_rtsp_camera_offline_happy_path(harness: Harness) -> None:
    response = await harness.client.post(f"{BASE}/front-door/probe")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"  # rtsp adapter: URL syntax check, no network needed
    assert isinstance(body["detail"], str) and body["detail"]


async def test_probe_unknown_camera_404(harness: Harness) -> None:
    response = await harness.client.post(f"{BASE}/ghost/probe")
    assert response.status_code == 404
    assert "GET /api/v1/config/cameras" in _problem(response.json())["detail"]


async def test_probe_unknown_adapter_422_names_available_adapters(harness: Harness) -> None:
    harness.add_effective_camera("porch", {"adapter": "acme-cloud"})
    response = await harness.client.post(f"{BASE}/porch/probe")
    assert response.status_code == 422
    detail = _problem(response.json())
    assert "acme-cloud" in detail["detail"]
    assert "rtsp" in detail["detail"] and "onvif" in detail["detail"]  # the available set


# --- auth scopes --------------------------------------------------------------------------------


async def test_write_endpoints_require_write_config_scope(harness: Harness) -> None:
    viewer = Principal(
        user_id=2,
        username="viewer",
        role="viewer",
        scopes=frozenset({"read:config"}),
        via="session",
    )
    harness.app.dependency_overrides[auth_deps.current_principal] = lambda: viewer
    assert (await harness.client.get(BASE)).status_code == 200
    assert (await harness.client.post(f"{BASE}/front-door/probe")).status_code == 200
    for response in (
        await harness.client.post(BASE, json={"id": "garage", "config": UI_CAM}),
        await harness.client.put(f"{BASE}/garage", json={"config": UI_CAM}),
        await harness.client.delete(f"{BASE}/garage"),
        await harness.client.post(f"{BASE}/discover"),
    ):
        assert response.status_code == 403
