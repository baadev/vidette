"""Keep-warm stream holders: one persistent gateway consumer per always-on camera.

Why this exists (field case: Eufy S3 Pro over NAS-RTSP): go2rtc dials the camera only
while at least one consumer is attached and hangs up when the last one leaves. Consumer
churn (a browser tab navigating away, the recorder restarting) therefore churns the
*camera* connection too. Eufy cameras serve exactly one RTSP client at a time and keep a
half-open session around after an unclean hangup — the next DESCRIBE gets "404 Stream Not
Found" until the camera times the ghost session out. Observed on a live deployment as
minutes-long "wrong response on DESCRIBE" storms and a live view stuck on snapshots.

The holder is a deliberately dumb MSE WebSocket client (`api/ws?src=<camera>`) that reads
and discards frames. Costs: one in-compose WebSocket and the camera's bitrate in local
traffic; no decode, no re-encode. Benefits: the producer stays connected, live view
attaches instantly, the recorder reconnects onto a hot stream, and single-client cameras
never see a second dial.

The holder doubles as the **zombie-producer watchdog**. Field case: the camera hung up
mid-stream (FIN), go2rtc kept the CLOSE_WAIT producer listed with frozen byte counters and
never re-dialed — while waiting consumers piled up (165 observed) and pinned the corpse.
From the holder's seat that looks like sessions that connect but deliver only the cached
fMP4 init segment (~2 KB) and then silence. After `_DRY_SESSIONS_BEFORE_RESET` such
sessions it asks the gateway to restart (config re-read + every session dropped — the only
reliable per-gateway kick, see `Go2rtcManager.restart_gateway`), rate-limited so a
genuinely offline camera cannot turn into a restart storm. Battery cameras have no holder,
so a sleeping camera is never "kicked" awake.

Cameras with `power_profile: battery` are skipped — holding their stream open would keep
them awake forever, which is exactly what that profile exists to prevent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import websockets

from vidette.core.config import PowerProfile, VidetteConfig
from vidette.streams.go2rtc import GatewayError, Go2rtcManager

logger = logging.getLogger(__name__)

SystemEventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]

# Same preference list the web player sends; go2rtc picks the first codec it can serve.
_MSE_CODECS = (
    "avc1.640029,avc1.64001F,avc1.4D401F,avc1.42E01F,"
    "hvc1.1.6.L153.B0,hev1.1.6.L153.B0,mp4a.40.2,mp4a.40.5,flac,opus"
)
_RECONNECT_MAX_S = 15.0  # mains cameras: recover fast, a dead producer costs live view
_IDLE_TIMEOUT_S = 30.0  # open socket but no frames → force a reconnect (re-dials the camera)
# A session that only ever delivers the cached init segment (~2 KB) is *dry* — the
# producer is dead behind it. Any real stream clears 16 KB in seconds (even 134 kbps
# video ships ~17 KB/s).
_ALIVE_MIN_BYTES = 16384
_DRY_SESSIONS_BEFORE_RESET = 3  # ≈100 s of confirmed silence before kicking the gateway
_RESET_MIN_INTERVAL_S = 120.0  # kicks are shared across cameras and rate-limited

HolderState = Literal["connecting", "warm", "reconnecting", "stopped"]


@dataclass
class KeepWarmStatus:
    camera: str
    state: HolderState
    bytes_received: int
    last_data_at: float | None


class StreamKeepWarm:
    """Owns one holder task per mains-powered camera; crash-only, stop() is clean."""

    def __init__(
        self,
        config: VidetteConfig,
        gateway: Go2rtcManager,
        *,
        on_event: SystemEventCallback | None = None,
        idle_timeout_s: float = _IDLE_TIMEOUT_S,  # test seam
        reset_min_interval_s: float = _RESET_MIN_INTERVAL_S,  # test seam
    ) -> None:
        self._config = config
        self._gateway = gateway
        self._on_event = on_event
        self._idle_timeout_s = idle_timeout_s
        self._reset_min_interval_s = reset_min_interval_s
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._states: dict[str, HolderState] = {}
        self._bytes: dict[str, int] = {}
        self._last_data: dict[str, float | None] = {}
        self._stopping = asyncio.Event()
        self._last_reset = 0.0  # monotonic; shared across cameras

    def status(self) -> dict[str, KeepWarmStatus]:
        return {
            camera: KeepWarmStatus(
                camera=camera,
                state=self._states.get(camera, "stopped"),
                bytes_received=self._bytes.get(camera, 0),
                last_data_at=self._last_data.get(camera),
            )
            for camera in self._tasks
        }

    async def start(self) -> None:
        self._stopping.clear()
        for camera_id, camera in self._config.cameras.items():
            if camera.power_profile is not PowerProfile.mains:
                continue
            if camera_id in self._gateway.skipped or camera_id in self._tasks:
                continue
            self._states[camera_id] = "connecting"
            self._bytes[camera_id] = 0
            self._last_data[camera_id] = None
            self._tasks[camera_id] = asyncio.create_task(
                self._hold(camera_id), name=f"vidette-keepwarm-{camera_id}"
            )

    async def stop(self) -> None:
        self._stopping.set()
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        self._states = dict.fromkeys(self._states, "stopped")

    # --- internals ------------------------------------------------------------------------

    async def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            await self._on_event(kind, payload)
        except Exception:
            logger.exception("keep-warm: event callback failed for %s", kind)

    async def _hold(self, camera_id: str) -> None:
        try:
            url = self._gateway.mse_ws_url(camera_id)
        except GatewayError as exc:  # camera disappeared between start() and here
            logger.warning("keep-warm %s: %s", camera_id, exc)
            self._states[camera_id] = "stopped"
            return
        backoff = 1.0
        dry_streak = 0
        while not self._stopping.is_set():
            session_bytes = await self._consume(camera_id, url)
            if self._stopping.is_set():
                break
            alive = session_bytes is not None and session_bytes >= _ALIVE_MIN_BYTES
            if alive:
                dry_streak = 0
                backoff = 1.0  # the stream works; re-attach immediately-ish
            else:
                backoff = min(_RECONNECT_MAX_S, backoff * 2)
                if session_bytes is not None:
                    # Connected but (near-)silent: the gateway answers, the camera does
                    # not — the zombie-producer signature. Connect *failures* don't
                    # count: a down gateway can't be fixed by restarting it from here.
                    dry_streak += 1
                    if dry_streak >= _DRY_SESSIONS_BEFORE_RESET:
                        dry_streak = 0
                        await self._maybe_reset_gateway(camera_id)
            self._states[camera_id] = "reconnecting"
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=backoff * random.uniform(0.8, 1.2)
                )
        self._states[camera_id] = "stopped"

    async def _maybe_reset_gateway(self, camera_id: str) -> None:
        """One shared, rate-limited gateway restart; failures are contained."""
        now = time.monotonic()
        if now - self._last_reset < self._reset_min_interval_s:
            return
        self._last_reset = now
        logger.warning(
            "keep-warm %s: stream connects but delivers no media — restarting the gateway "
            "to drop the zombie camera session",
            camera_id,
        )
        await self._emit(
            "stream.gateway_reset",
            {
                "camera": camera_id,
                "reason": "stream sessions connect but deliver no media "
                "(camera hung up; the gateway kept a dead session)",
            },
        )
        try:
            await self._gateway.restart_gateway()
        except GatewayError as exc:
            logger.warning("keep-warm %s: gateway restart failed: %s", camera_id, exc)

    async def _consume(self, camera_id: str, url: str) -> int | None:
        """One connect-and-drain session.

        Returns the media bytes received, or None when the connection never came up.
        Never raises (except CancelledError): every session ends either in a clean idle
        timeout — go2rtc keeps quiet sockets open even when its producer is gone, so
        silence must force a re-dial — or in a connection error; both mean "reconnect".
        """
        session_bytes: int | None = None
        self._states[camera_id] = "connecting"
        try:
            async with websockets.connect(url, max_size=None, open_timeout=5) as upstream:
                session_bytes = 0
                await upstream.send('{"type":"mse","value":"' + _MSE_CODECS + '"}')
                while not self._stopping.is_set():
                    frame = await asyncio.wait_for(
                        upstream.recv(), timeout=self._idle_timeout_s
                    )
                    if isinstance(frame, str):
                        continue  # control JSON (codec handshake)
                    session_bytes += len(frame)
                    self._bytes[camera_id] = self._bytes.get(camera_id, 0) + len(frame)
                    if session_bytes >= _ALIVE_MIN_BYTES:
                        self._states[camera_id] = "warm"
                        self._last_data[camera_id] = time.time()
        except (OSError, websockets.exceptions.WebSocketException, TimeoutError) as exc:
            logger.debug("keep-warm %s: %s", camera_id, exc)
        return session_bytes
