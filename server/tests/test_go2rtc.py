"""Tests for the go2rtc gateway manager (vidette.streams.go2rtc)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import yaml

from vidette.adapters.base import (
    AdapterInfo,
    AdapterNotReadyError,
    Capability,
    StreamEndpoint,
)
from vidette.core.config import CameraConfig, CameraSource, VidetteConfig
from vidette.streams import go2rtc as go2rtc_module
from vidette.streams.go2rtc import GatewayError, Go2rtcManager

MAIN_URL = "rtsp://user:pw@203.0.113.10:554/stream1"
SUB_URL = "rtsp://user:pw@203.0.113.10:554/stream2"


def make_manager(
    config: VidetteConfig,
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
    *,
    config_path: Path | None = None,
) -> Go2rtcManager:
    transport = httpx.MockTransport(handler) if handler is not None else None
    return Go2rtcManager(config, config_path=config_path, transport=transport)


# --- build_config -----------------------------------------------------------------------------


async def test_build_config_maps_main_and_sub(test_config: VidetteConfig) -> None:
    manager = make_manager(test_config)
    built = await manager.build_config()
    await manager.close()

    assert built["streams"]["front-door"] == [MAIN_URL]
    assert built["streams"]["front-door__sub"] == [SUB_URL]
    assert built["api"] == {"listen": ":1984"}
    assert built["rtsp"] == {"listen": ":8554"}
    assert built["webrtc"] == {"listen": ":8555"}
    assert manager.skipped == {}


async def test_build_config_skips_not_ready_adapter(
    test_config: VidetteConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A designed-but-inert bridge adapter (docs/architecture/plugins.md) must be skipped
    with its reason recorded — never fatal for the healthy cameras."""

    class NotReadyAdapter:
        info = AdapterInfo(
            id="future-bridge",
            display_name="Future bridge",
            maturity="designed",
            capabilities=Capability.NONE,
            docs_url="https://example.invalid/docs",
        )

        async def stream_endpoints(
            self, camera_id: str, config: CameraConfig
        ) -> list[StreamEndpoint]:
            raise AdapterNotReadyError("future-bridge is designed — see its docs page")

    real_available = go2rtc_module.available_adapters

    def patched() -> dict[str, object]:
        registry: dict[str, object] = dict(real_available())
        registry["future-bridge"] = NotReadyAdapter()
        return registry

    monkeypatch.setattr(go2rtc_module, "available_adapters", patched)

    config = test_config.model_copy(deep=True)
    config.cameras["backyard"] = CameraConfig(adapter="future-bridge")
    manager = make_manager(config)
    built = await manager.build_config()
    await manager.close()

    assert "backyard" not in built["streams"]
    assert "backyard__sub" not in built["streams"]
    assert built["streams"]["front-door"] == [MAIN_URL]  # healthy cameras unaffected
    assert "backyard" in manager.skipped
    assert "designed" in manager.skipped["backyard"]


async def test_build_config_skips_unknown_adapter(test_config: VidetteConfig) -> None:
    config = test_config.model_copy(deep=True)
    config.cameras["attic"] = CameraConfig(adapter="no-such-adapter")
    manager = make_manager(config)
    built = await manager.build_config()
    await manager.close()

    assert "attic" not in built["streams"]
    assert "no-such-adapter" in manager.skipped["attic"]
    assert "rtsp" in manager.skipped["attic"]  # lists the available adapters


async def test_build_config_skips_misconfigured_source(test_config: VidetteConfig) -> None:
    config = test_config.model_copy(deep=True)
    config.cameras["garage"] = CameraConfig(
        adapter="rtsp", source=CameraSource(main="http://not-rtsp.example/stream")
    )
    manager = make_manager(config)
    built = await manager.build_config()
    await manager.close()

    assert "garage" not in built["streams"]
    assert "rtsp://" in manager.skipped["garage"]  # tells the user what a valid URL looks like


# --- sync -------------------------------------------------------------------------------------


async def test_sync_writes_config_and_restarts_gateway(
    test_config: VidetteConfig, tmp_path: Path
) -> None:
    restart_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/restart":
            restart_calls.append(request.method)
            return httpx.Response(200)
        return httpx.Response(404)

    path = tmp_path / "go2rtc.yaml"
    manager = make_manager(test_config, handler, config_path=path)

    assert await manager.sync() is True
    written = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert written["streams"]["front-door"] == [MAIN_URL]
    assert written["streams"]["front-door__sub"] == [SUB_URL]
    assert written["api"] == {"listen": ":1984"}
    assert restart_calls == ["POST"]

    # Unchanged config: no rewrite, no restart.
    assert await manager.sync() is False
    assert restart_calls == ["POST"]
    await manager.close()


async def test_sync_detects_config_change(test_config: VidetteConfig, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    path = tmp_path / "go2rtc.yaml"
    first = make_manager(test_config, handler, config_path=path)
    assert await first.sync() is True
    await first.close()

    changed = test_config.model_copy(deep=True)
    changed.cameras["garage"] = CameraConfig(
        adapter="rtsp", source=CameraSource(main="rtsp://203.0.113.11:554/stream1")
    )
    second = make_manager(changed, handler, config_path=path)
    assert await second.sync() is True
    written = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert written["streams"]["garage"] == ["rtsp://203.0.113.11:554/stream1"]
    await second.close()


async def test_sync_without_config_path_returns_false(test_config: VidetteConfig) -> None:
    manager = make_manager(test_config)
    assert await manager.sync() is False
    assert manager.skipped == {}  # build still ran and refreshed the skip report
    await manager.close()


async def test_sync_survives_unreachable_gateway(
    test_config: VidetteConfig, tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    path = tmp_path / "go2rtc.yaml"
    manager = make_manager(test_config, handler, config_path=path)
    assert await manager.sync() is True  # file written; restart failure is swallowed
    assert path.exists()
    await manager.close()


# --- health -----------------------------------------------------------------------------------


async def test_health_reports_version_and_streams(test_config: VidetteConfig) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api":
            return httpx.Response(200, json={"version": "1.9.4"})
        if request.url.path == "/api/streams":
            return httpx.Response(200, json={"front-door": {}, "front-door__sub": {}})
        return httpx.Response(404)

    manager = make_manager(test_config, handler)
    health = await manager.health()
    await manager.close()

    assert health.reachable is True
    assert health.version == "1.9.4"
    assert health.streams == frozenset({"front-door", "front-door__sub"})
    assert health.detail == ""


async def test_health_unreachable_gateway_is_actionable(test_config: VidetteConfig) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    manager = make_manager(test_config, handler)
    health = await manager.health()
    await manager.close()

    assert health.reachable is False
    assert health.version is None
    assert health.streams == frozenset()
    assert "VIDETTE_GO2RTC_URL" in health.detail
    assert "http://go2rtc:1984" in health.detail


async def test_health_http_error_is_unreachable(test_config: VidetteConfig) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    manager = make_manager(test_config, handler)
    health = await manager.health()
    await manager.close()

    assert health.reachable is False
    assert "VIDETTE_GO2RTC_URL" in health.detail


# --- whep_exchange ----------------------------------------------------------------------------


async def test_whep_exchange_proxies_offer_and_answer(test_config: VidetteConfig) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["src"] = request.url.params.get("src")
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = request.content
        return httpx.Response(201, text="v=0\r\nanswer-sdp")

    manager = make_manager(test_config, handler)
    answer = await manager.whep_exchange("front-door", "v=0\r\noffer-sdp")
    await manager.close()

    assert answer == "v=0\r\nanswer-sdp"
    assert seen["path"] == "/api/webrtc"
    assert seen["src"] == "front-door"
    assert seen["content_type"] == "application/sdp"
    assert seen["body"] == b"v=0\r\noffer-sdp"


async def test_whep_exchange_rejected_offer_raises_actionable_error(
    test_config: VidetteConfig,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="stream not found")

    manager = make_manager(test_config, handler)
    with pytest.raises(GatewayError) as excinfo:
        await manager.whep_exchange("front-door", "v=0")
    await manager.close()

    message = str(excinfo.value)
    assert "500" in message
    assert "front-door" in message
    assert "/api/streams" in message  # tells the user where to look next


async def test_whep_exchange_unreachable_gateway(test_config: VidetteConfig) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    manager = make_manager(test_config, handler)
    with pytest.raises(GatewayError) as excinfo:
        await manager.whep_exchange("front-door", "v=0")
    await manager.close()

    assert "VIDETTE_GO2RTC_URL" in str(excinfo.value)


async def test_whep_exchange_unknown_camera(test_config: VidetteConfig) -> None:
    manager = make_manager(test_config)
    with pytest.raises(GatewayError) as excinfo:
        await manager.whep_exchange("nope", "v=0")
    await manager.close()

    message = str(excinfo.value)
    assert "unknown camera 'nope'" in message
    assert "front-door" in message  # lists the configured cameras


# --- snapshot ---------------------------------------------------------------------------------


async def test_snapshot_returns_jpeg_bytes(test_config: VidetteConfig) -> None:
    jpeg = b"\xff\xd8\xff\xe0fakejpeg"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/frame.jpeg"
        assert request.url.params.get("src") == "front-door"
        return httpx.Response(200, content=jpeg, headers={"content-type": "image/jpeg"})

    manager = make_manager(test_config, handler)
    frame = await manager.snapshot("front-door")
    await manager.close()
    assert frame == jpeg


async def test_snapshot_failure_is_actionable(test_config: VidetteConfig) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="unknown stream")

    manager = make_manager(test_config, handler)
    with pytest.raises(GatewayError) as excinfo:
        await manager.snapshot("front-door")
    await manager.close()

    message = str(excinfo.value)
    assert "404" in message
    assert "front-door" in message


async def test_snapshot_unknown_camera(test_config: VidetteConfig) -> None:
    manager = make_manager(test_config)
    with pytest.raises(GatewayError, match="unknown camera 'nope'"):
        await manager.snapshot("nope")
    await manager.close()


# --- restream_url -----------------------------------------------------------------------------


async def test_restream_url_main_and_sub(test_config: VidetteConfig) -> None:
    manager = make_manager(test_config)
    assert manager.restream_url("front-door") == "rtsp://go2rtc:8554/front-door"
    assert manager.restream_url("front-door", "main") == "rtsp://go2rtc:8554/front-door"
    assert manager.restream_url("front-door", "sub") == "rtsp://go2rtc:8554/front-door__sub"
    await manager.close()


async def test_restream_url_respects_custom_base(test_config: VidetteConfig) -> None:
    manager = Go2rtcManager(test_config, rtsp_base="rtsp://127.0.0.1:9554/")
    assert manager.restream_url("front-door") == "rtsp://127.0.0.1:9554/front-door"
    await manager.close()


async def test_restream_url_unknown_camera(test_config: VidetteConfig) -> None:
    manager = make_manager(test_config)
    with pytest.raises(GatewayError, match="unknown camera 'nope'"):
        manager.restream_url("nope")
    await manager.close()
