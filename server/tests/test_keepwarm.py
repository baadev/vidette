"""Keep-warm holder tests: a fake go2rtc MSE WebSocket in the same asyncio loop.

Unlike test_mse_proxy (which needs a thread because of TestClient's portal loop), the
holder is plain asyncio — the fake gateway runs right here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace
from typing import Any

from websockets.asyncio.server import ServerConnection, serve

from vidette.core.config import VidetteConfig
from vidette.streams.go2rtc import GatewayError
from vidette.streams.keepwarm import StreamKeepWarm

# Real media clears the 16 KB per-session liveness bar; the init segment (~2 KB) doesn't.
MEDIA_CHUNK = b"\x00\x00\x00\x18ftypiso5" + b"\x01" * 8192
INIT_CHUNK = b"\x00\x00\x00\x18ftypiso5" + b"\x01" * 128


def _config(cameras: dict[str, dict[str, Any]]) -> VidetteConfig:
    return VidetteConfig.model_validate({"cameras": cameras})


def _gateway(port: int, skipped: dict[str, str] | None = None) -> Any:
    restarts: list[float] = []

    def mse_ws_url(camera_id: str) -> str:
        if camera_id == "ghost":
            raise GatewayError("unknown camera 'ghost'")
        return f"ws://127.0.0.1:{port}/api/ws?src={camera_id}"

    async def restart_gateway() -> None:
        restarts.append(asyncio.get_running_loop().time())

    return SimpleNamespace(
        skipped=skipped or {},
        mse_ws_url=mse_ws_url,
        restart_gateway=restart_gateway,
        restarts=restarts,
    )


async def test_keepwarm_drains_frames_and_reconnects() -> None:
    """The holder sends the MSE init, drains frames (state → warm, bytes counted), and
    dials again when the gateway drops the connection — that re-dial is the whole point:
    it forces go2rtc to keep its camera producer alive."""
    connections = 0
    inits: list[str] = []
    connected = asyncio.Event()
    reconnected = asyncio.Event()

    async def handler(connection: ServerConnection) -> None:
        nonlocal connections
        connections += 1
        if connections >= 2:
            reconnected.set()
        init = await connection.recv()
        assert isinstance(init, str)
        inits.append(init)
        await connection.send('{"type":"mse","value":"avc1.64001F"}')
        for _ in range(3):
            await connection.send(MEDIA_CHUNK)
        connected.set()
        # Close from the server side → the holder must reconnect.

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        gateway = _gateway(port)
        holder = StreamKeepWarm(
            _config({"front-door": {"source": {"main": "rtsp://cam/1"}}}), gateway
        )
        await holder.start()
        try:
            await asyncio.wait_for(connected.wait(), timeout=5)
            await asyncio.wait_for(reconnected.wait(), timeout=5)
            status = holder.status()["front-door"]
        finally:
            await holder.stop()

    assert connections >= 2
    init = json.loads(inits[0])
    assert init["type"] == "mse" and "avc1" in init["value"]
    assert status.bytes_received >= 3 * len(MEDIA_CHUNK)
    assert status.last_data_at is not None
    assert gateway.restarts == []  # media flowed — nothing to kick
    assert holder.status() == {}  # stop() left nothing running


async def test_keepwarm_only_holds_mains_cameras() -> None:
    """battery cameras must sleep (no holder); gateway-skipped cameras have no stream."""
    holder = StreamKeepWarm(
        _config(
            {
                "porch": {"source": {"main": "rtsp://cam/1"}},  # mains (default)
                "gate": {"source": {"main": "rtsp://cam/2"}, "power_profile": "battery"},
                "broken": {"source": {"main": "rtsp://cam/3"}},
            }
        ),
        _gateway(1, skipped={"broken": "adapter not ready"}),
    )
    await holder.start()
    try:
        assert set(holder.status()) == {"porch"}
    finally:
        await holder.stop()


async def test_keepwarm_kicks_gateway_after_dry_sessions() -> None:
    """Sessions that connect but deliver only the cached init segment are the zombie-
    producer signature (field case: camera FIN'd, go2rtc kept a CLOSE_WAIT corpse): after
    3 such sessions the holder restarts the gateway — once, rate-limited."""
    kicked = asyncio.Event()
    events: list[tuple[str, dict[str, Any]]] = []

    async def handler(connection: ServerConnection) -> None:
        await connection.recv()  # the MSE init
        await connection.send('{"type":"mse","value":"avc1.64001F"}')
        await connection.send(INIT_CHUNK)  # cached init only, then silence
        with contextlib.suppress(Exception):
            await asyncio.sleep(30)  # never send media; the holder must time out

    async def on_event(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, payload))
        kicked.set()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        gateway = _gateway(port)
        holder = StreamKeepWarm(
            _config({"front-door": {"source": {"main": "rtsp://cam/1"}}}),
            gateway,
            on_event=on_event,
            idle_timeout_s=0.1,  # a dry session ends in ~100 ms instead of 30 s
            reset_min_interval_s=60.0,  # only one kick within this test's lifetime
        )
        await holder.start()
        try:
            await asyncio.wait_for(kicked.wait(), timeout=10)
            await asyncio.sleep(0.5)  # a few more dry sessions — must NOT kick again
        finally:
            await holder.stop()

    assert len(gateway.restarts) == 1
    assert [k for k, _ in events] == ["stream.gateway_reset"]
    assert events[0][1]["camera"] == "front-door"


async def test_keepwarm_survives_gateway_down_without_kicking() -> None:
    """Nothing listening: connect *failures* keep retrying quietly and never trigger a
    gateway restart (a down gateway cannot be fixed from here)."""
    gateway = _gateway(1)  # port 1 — connection refused
    holder = StreamKeepWarm(
        _config({"front-door": {"source": {"main": "rtsp://cam/1"}}}),
        gateway,
        idle_timeout_s=0.05,
        reset_min_interval_s=0.0,
    )
    await holder.start()
    await asyncio.sleep(0.5)  # several failed connects
    status = holder.status()["front-door"]
    assert status.state in ("connecting", "reconnecting")
    assert status.bytes_received == 0
    assert gateway.restarts == []
    await holder.stop()
    assert holder.status() == {}
