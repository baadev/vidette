"""Generic RTSP adapter — Vidette's native tongue (docs/cameras/onvif-rtsp.md).

M0 scope: configuration validation with actionable diagnostics. M1 adds the network probe
and go2rtc wiring; the endpoint contract is already final.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import urlparse

from vidette.core.config import CameraConfig
from vidette.core.events import Observation

from .base import (
    AdapterInfo,
    Capability,
    ProbeResult,
    ProbeStatus,
    StreamEndpoint,
)

_DOCS = "https://github.com/baadev/vidette/blob/main/docs/cameras/onvif-rtsp.md"


class RtspAdapter:
    info = AdapterInfo(
        id="rtsp",
        display_name="Generic RTSP",
        maturity="designed",  # flips to beta when M1 streaming lands
        capabilities=Capability.LIVE_MAIN | Capability.LIVE_SUB,
        docs_url=_DOCS,
    )

    async def probe(self, camera_id: str, config: CameraConfig) -> ProbeResult:
        if config.source is None:
            return ProbeResult(
                ProbeStatus.misconfigured,
                f"camera '{camera_id}': the rtsp adapter requires 'source.main' — see {_DOCS}",
            )
        for role, url in (("main", config.source.main), ("sub", config.source.sub)):
            if url is None:
                continue
            parsed = urlparse(url)
            if parsed.scheme not in ("rtsp", "rtsps"):
                return ProbeResult(
                    ProbeStatus.misconfigured,
                    f"camera '{camera_id}': source.{role} must be an rtsp:// or rtsps:// URL "
                    f"(got scheme '{parsed.scheme or 'none'}') — see {_DOCS}",
                )
            if not parsed.hostname:
                return ProbeResult(
                    ProbeStatus.misconfigured,
                    f"camera '{camera_id}': source.{role} is missing a host",
                )
        if config.source.sub is None:
            return ProbeResult(
                ProbeStatus.ok,
                "URL syntax valid. No substream configured — analysis will decode the main "
                "stream, which costs significantly more CPU; configure 'source.sub' if the "
                f"camera offers one ({_DOCS}). Network reachability probing lands in M1.",
            )
        return ProbeResult(
            ProbeStatus.ok,
            "URL syntax valid. Network reachability probing lands in M1.",
        )

    async def stream_endpoints(self, camera_id: str, config: CameraConfig) -> list[StreamEndpoint]:
        probe = await self.probe(camera_id, config)
        if not probe.ok:
            raise ValueError(probe.detail)
        assert config.source is not None  # guaranteed by probe
        endpoints = [StreamEndpoint(role="main", url=config.source.main)]
        if config.source.sub:
            endpoints.append(StreamEndpoint(role="sub", url=config.source.sub))
        return endpoints

    async def observations(
        self, camera_id: str, config: CameraConfig
    ) -> AsyncIterator[Observation]:
        """Plain RTSP has no vendor push channel; ONVIF events arrive with the onvif adapter."""
        nothing: tuple[Observation, ...] = ()
        for observation in nothing:  # empty async generator, typed and mypy-clean
            yield observation
