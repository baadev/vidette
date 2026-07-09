"""go2rtc manager.

Design (ADR-0002): go2rtc runs as a sidecar; Vidette *manages its configuration* and talks
to its HTTP API. The go2rtc admin API is never exposed to browsers — WHEP and snapshots are
proxied through Vidette's authenticated API (see api/routers/streams.py).

Implementation notes:
- `build_config()` maps `cameras:` to go2rtc streams: main stream under the camera id,
  substream under "<camera>__sub". Only adapters that provide RTSP-ish sources contribute
  at M1 (use `vidette.adapters.base.available_adapters()` → `stream_endpoints`; adapters
  raising AdapterNotReadyError are skipped with a note, recorded in `Go2rtcManager.skipped`).
- `sync()` renders YAML, writes atomically to `config_path` only on change, then asks a
  reachable gateway to reload via POST /api/restart. Never raises on gateway absence —
  reports through `health()`.
- HTTP via httpx.AsyncClient (timeout 5 s). WHEP exchange: POST {base}/api/webrtc?src=<id>
  with the SDP offer (content-type application/sdp), returns the answer SDP.
- Snapshot: GET {base}/api/frame.jpeg?src=<id> → bytes.
- Camera ids are validated against the config before touching URLs (defense in depth).
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml

from vidette.adapters.base import AdapterError, available_adapters
from vidette.core.config import VidetteConfig

DEFAULT_API_URL = "http://go2rtc:1984"
DEFAULT_RTSP_BASE = "rtsp://go2rtc:8554"

_SUB_SUFFIX = "__sub"
_TIMEOUT_SECONDS = 5.0


class GatewayError(Exception):
    """Gateway interaction failed; message is user-actionable."""


@dataclass(frozen=True)
class GatewayHealth:
    reachable: bool
    version: str | None
    streams: frozenset[str] = field(default_factory=frozenset)
    detail: str = ""


def _json_or_none(response: httpx.Response) -> Any:
    """Lenient JSON parse — a gateway answering non-JSON is still *reachable*."""
    try:
        return response.json()
    except ValueError:
        return None


def _body_snippet(response: httpx.Response) -> str:
    return response.text.strip()[:200] or "<empty body>"


class Go2rtcManager:
    """Owns the generated go2rtc config and all HTTP traffic to the gateway.

    Attributes:
        skipped: camera id → human-readable reason it was left out of the last generated
            config (adapter not ready, misconfigured source, unknown adapter). Refreshed
            by every `build_config()` / `sync()` call; surfaced via the API for honesty.
    """

    def __init__(
        self,
        config: VidetteConfig,
        *,
        api_url: str = DEFAULT_API_URL,
        rtsp_base: str = DEFAULT_RTSP_BASE,
        config_path: Path | None = None,
        webrtc_candidates: Sequence[str] = (),
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """`transport` is a keyword-only test seam (httpx.MockTransport); never set in prod."""
        self.config = config
        self.api_url = api_url.rstrip("/")
        self.rtsp_base = rtsp_base.rstrip("/")
        self.config_path = config_path
        self.webrtc_candidates = tuple(webrtc_candidates)
        self.skipped: dict[str, str] = {}
        self._client = httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, transport=transport)

    def mse_ws_url(self, camera_id: str) -> str:
        """go2rtc's MSE WebSocket for a camera — consumed by Vidette's authenticated proxy
        (the gateway API is never exposed to browsers directly)."""
        if camera_id not in self.config.cameras:
            raise GatewayError(f"unknown camera '{camera_id}'")
        ws_base = self.api_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        return f"{ws_base}/api/ws?src={camera_id}"

    async def build_config(self) -> dict[str, Any]:
        """VidetteConfig → go2rtc config dict (streams + api/rtsp/webrtc listeners).

        Async because stream endpoints come from adapters (`stream_endpoints`); adapters
        raising AdapterNotReadyError are skipped, not fatal.
        """
        adapters = available_adapters()
        streams: dict[str, list[str]] = {}
        skipped: dict[str, str] = {}
        for camera_id, camera in self.config.cameras.items():
            adapter = adapters.get(camera.adapter)
            if adapter is None:
                known = ", ".join(sorted(adapters)) or "none installed"
                skipped[camera_id] = (
                    f"unknown adapter '{camera.adapter}' (available: {known}) — fix "
                    f"'cameras.{camera_id}.adapter' in your config or install the adapter package"
                )
                continue
            try:
                endpoints = await adapter.stream_endpoints(camera_id, camera)
            except (AdapterError, ValueError) as exc:
                skipped[camera_id] = str(exc)
                continue
            for endpoint in endpoints:
                name = camera_id if endpoint.role == "main" else f"{camera_id}{_SUB_SUFFIX}"
                streams[name] = [endpoint.url]
        self.skipped = skipped
        webrtc: dict[str, Any] = {"listen": ":8555"}
        if self.webrtc_candidates:
            webrtc["candidates"] = list(self.webrtc_candidates)
        return {
            "api": {"listen": ":1984"},
            "rtsp": {"listen": ":8554"},
            "webrtc": webrtc,
            "streams": streams,
        }

    async def sync(self) -> bool:
        """Write config file if changed; hot-reload a reachable gateway. Returns changed."""
        desired = await self.build_config()
        if self.config_path is None:
            return False  # nothing to write; in-memory managers (tests, wizard) end here
        rendered = yaml.safe_dump(desired, sort_keys=True)
        path = self.config_path
        try:
            current: str | None = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            current = None
        except OSError as exc:
            raise GatewayError(
                f"cannot read the generated go2rtc config at {path}: {exc} — check the file "
                "permissions or point VIDETTE_GO2RTC_CONF at a writable location"
            ) from exc
        if current == rendered:
            return False
        tmp_path = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(rendered, encoding="utf-8")
            os.replace(tmp_path, path)
        except OSError as exc:
            raise GatewayError(
                f"cannot write the go2rtc config to {path}: {exc} — check the directory exists "
                "and is writable, or point VIDETTE_GO2RTC_CONF at a writable location"
            ) from exc
        # Best-effort hot reload: gateway absence is reported by health(), never raised here.
        with contextlib.suppress(httpx.HTTPError):
            await self._client.post(f"{self.api_url}/api/restart")
        return True

    async def health(self) -> GatewayHealth:
        try:
            info = await self._client.get(f"{self.api_url}/api")
            info.raise_for_status()
            streams_response = await self._client.get(f"{self.api_url}/api/streams")
            streams_response.raise_for_status()
        except httpx.HTTPError as exc:
            reason = str(exc) or exc.__class__.__name__
            return GatewayHealth(
                reachable=False,
                version=None,
                detail=(
                    f"go2rtc gateway not reachable at {self.api_url} ({reason}) — check that "
                    "the go2rtc sidecar container is running and that VIDETTE_GO2RTC_URL "
                    "points at its API"
                ),
            )
        version: str | None = None
        payload = _json_or_none(info)
        if isinstance(payload, dict):
            raw_version = payload.get("version")
            if isinstance(raw_version, str):
                version = raw_version
        names: frozenset[str] = frozenset()
        streams_payload = _json_or_none(streams_response)
        if isinstance(streams_payload, dict):
            names = frozenset(str(name) for name in streams_payload)
        return GatewayHealth(reachable=True, version=version, streams=names)

    def restream_url(self, camera_id: str, role: Literal["main", "sub"] = "main") -> str:
        """rtsp://<gateway>/<camera_id>[__sub] — the recorder's source (one camera connection)."""
        self._ensure_camera(camera_id)
        name = camera_id if role == "main" else f"{camera_id}{_SUB_SUFFIX}"
        return f"{self.rtsp_base}/{name}"

    async def whep_exchange(self, camera_id: str, offer_sdp: str) -> str:
        """Proxy a WHEP offer/answer for the browser. Raises GatewayError."""
        self._ensure_camera(camera_id)
        try:
            response = await self._client.post(
                f"{self.api_url}/api/webrtc",
                params={"src": camera_id},
                content=offer_sdp,
                headers={"content-type": "application/sdp"},
            )
        except httpx.HTTPError as exc:
            raise GatewayError(
                f"WHEP exchange for camera '{camera_id}' failed: cannot reach go2rtc at "
                f"{self.api_url} ({str(exc) or exc.__class__.__name__}) — check the go2rtc sidecar "
                "is running and VIDETTE_GO2RTC_URL is correct"
            ) from exc
        if not response.is_success:
            raise GatewayError(
                f"go2rtc rejected the WHEP offer for camera '{camera_id}' "
                f"(HTTP {response.status_code}): {_body_snippet(response)} — verify the stream "
                f"exists (GET {self.api_url}/api/streams) and re-run config sync if it is missing"
            )
        return response.text

    async def snapshot(self, camera_id: str) -> bytes:
        """JPEG frame for previews/wizard verification. Raises GatewayError."""
        self._ensure_camera(camera_id)
        try:
            response = await self._client.get(
                f"{self.api_url}/api/frame.jpeg", params={"src": camera_id}
            )
        except httpx.HTTPError as exc:
            raise GatewayError(
                f"snapshot for camera '{camera_id}' failed: cannot reach go2rtc at "
                f"{self.api_url} ({str(exc) or exc.__class__.__name__}) — check the go2rtc sidecar "
                "is running and VIDETTE_GO2RTC_URL is correct"
            ) from exc
        if not response.is_success:
            raise GatewayError(
                f"go2rtc could not produce a frame for camera '{camera_id}' "
                f"(HTTP {response.status_code}): {_body_snippet(response)} — the camera may be "
                "offline or its stream not yet synced; check the gateway health and the "
                "camera's source URL"
            )
        return response.content

    async def restart_gateway(self) -> None:
        """POST /api/restart: reload the gateway's config and drop every session.

        The reliable kick for zombie camera sessions (field case: a Eufy hung up
        mid-stream, go2rtc kept the CLOSE_WAIT producer listed forever, and the piled-up
        waiting consumers pinned it — the camera's single RTSP slot stayed poisoned).
        Scoped per-stream DELETE+PUT is not viable: go2rtc's PUT persists by patching its
        YAML config file and chokes on our generated one ("did not find expected '-'
        indicator"). A restart re-reads that file — which Vidette owns — so state is
        rebuilt exactly from the source of truth.
        """
        try:
            response = await self._client.post(f"{self.api_url}/api/restart")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise GatewayError(
                f"gateway restart failed: cannot reach go2rtc at {self.api_url} "
                f"({str(exc) or exc.__class__.__name__}) — check the go2rtc sidecar is running "
                "and VIDETTE_GO2RTC_URL is correct"
            ) from exc

    async def close(self) -> None:
        await self._client.aclose()

    def _ensure_camera(self, camera_id: str) -> None:
        if camera_id not in self.config.cameras:
            known = ", ".join(sorted(self.config.cameras)) or "none configured"
            raise GatewayError(
                f"unknown camera '{camera_id}' — configured cameras: {known}. Add it under "
                "'cameras:' in your Vidette config or fix the id in the request."
            )
