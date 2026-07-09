"""MSE relay tests: browser WS ⇄ vidette proxy ⇄ (fake) go2rtc WS.

The fake gateway runs a real websockets server on its own thread/loop so the TestClient's
portal loop and the pytest loop never deadlock on each other.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import websockets
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.server import ServerConnection

from vidette.api.routers.streams import router as streams_router
from vidette.api.routers.streams import ws_router as streams_ws_router
from vidette.core.config import VidetteConfig
from vidette.streams.go2rtc import GatewayError

MSE_INIT = '{"type":"mse","value":"avc1.640029,mp4a.40.2"}'
MSE_REPLY = '{"type":"mse","value":"avc1.64001F"}'
FMP4_CHUNK = b"\x00\x00\x00\x18ftypiso5" + b"\x01" * 32


class FakeGateway:
    """Runs a websockets server on a private thread; records what the proxy sent it."""

    def __init__(self) -> None:
        self.received: list[str | bytes] = []
        self._loop = asyncio.new_event_loop()
        self._started = threading.Event()
        self._shutdown: asyncio.Event | None = None
        self.port: int = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._started.wait(5), "fake gateway failed to start"

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)

        async def handler(connection: ServerConnection) -> None:
            init = await connection.recv()
            self.received.append(init)
            await connection.send(MSE_REPLY)
            await connection.send(FMP4_CHUNK)
            # Stay open until the peer (the proxy) hangs up.
            async for frame in connection:
                self.received.append(frame)

        async def main() -> None:
            self._shutdown = asyncio.Event()
            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                self.port = server.sockets[0].getsockname()[1]
                self._started.set()
                await self._shutdown.wait()

        self._loop.run_until_complete(main())
        self._loop.close()

    def stop(self) -> None:
        if self._shutdown is not None:
            self._loop.call_soon_threadsafe(self._shutdown.set)
        self._thread.join(timeout=5)


def _app(tmp_path: Path, gateway_port: int, auth_mode: str) -> FastAPI:
    config = VidetteConfig.model_validate(
        {
            "server": {"auth": {"mode": auth_mode}},
            "storage": {
                "media_dir": str(tmp_path / "media"),
                "database": str(tmp_path / "db.sqlite"),
            },
            "cameras": {"front-door": {"adapter": "rtsp", "source": {"main": "rtsp://cam/1"}}},
        }
    )

    def mse_ws_url(camera_id: str) -> str:
        if camera_id not in config.cameras:
            raise GatewayError(f"unknown camera '{camera_id}'")
        return f"ws://127.0.0.1:{gateway_port}/api/ws?src={camera_id}"

    class FakeAuth:
        async def authenticate_bearer(self, token: str) -> Any:
            return None

        async def authenticate_session(self, token: str) -> Any:
            return None

    app = FastAPI()
    app.include_router(streams_router)
    app.include_router(streams_ws_router)
    app.state.runtime = SimpleNamespace(
        config=config,
        auth=FakeAuth(),
        go2rtc=SimpleNamespace(mse_ws_url=mse_ws_url),
    )
    return app


@pytest.fixture
def fake_gateway() -> Iterator[FakeGateway]:
    gateway = FakeGateway()
    yield gateway
    gateway.stop()


def test_mse_relay_pumps_text_and_binary(tmp_path: Path, fake_gateway: FakeGateway) -> None:
    app = _app(tmp_path, fake_gateway.port, auth_mode="none")
    client = TestClient(app)
    with client.websocket_connect("/api/v1/streams/front-door/mse") as ws:
        ws.send_text(MSE_INIT)  # browser → gateway: codec negotiation
        assert ws.receive_text() == MSE_REPLY  # gateway → browser: chosen codec
        assert ws.receive_bytes() == FMP4_CHUNK  # gateway → browser: media
    assert fake_gateway.received[0] == MSE_INIT


def test_mse_relay_unknown_camera_closes_4404(
    tmp_path: Path, fake_gateway: FakeGateway
) -> None:
    app = _app(tmp_path, fake_gateway.port, auth_mode="none")
    client = TestClient(app)
    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/api/v1/streams/ghost/mse"),
    ):
        pass  # rejected before accept — entering the context raises
    assert excinfo.value.code == 4404


def test_mse_relay_unauthenticated_closes_4401(
    tmp_path: Path, fake_gateway: FakeGateway
) -> None:
    app = _app(tmp_path, fake_gateway.port, auth_mode="builtin")
    client = TestClient(app)
    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/api/v1/streams/front-door/mse"),
    ):
        pass
    assert excinfo.value.code == 4401


def test_mse_relay_gateway_down_closes_4502(tmp_path: Path) -> None:
    app = _app(tmp_path, gateway_port=1, auth_mode="none")  # nothing listens on port 1
    client = TestClient(app)
    with client.websocket_connect("/api/v1/streams/front-door/mse") as ws:
        with pytest.raises(Exception) as excinfo:
            ws.receive_text()
        assert "4502" in str(excinfo.value) or getattr(excinfo.value, "code", None) == 4502
