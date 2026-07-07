"""Eufy adapter — designed for M2, documented today (docs/cameras/eufy.md).

Path: thin client over the community `eufy-security-ws` sidecar (bropat), which owns cloud
auth + P2P. This module currently validates configuration and states its status honestly;
it must never pretend to stream.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from pydantic import BaseModel, ConfigDict, ValidationError

from vidette.core.config import CameraConfig
from vidette.core.events import Observation

from .base import (
    AdapterInfo,
    AdapterNotReadyError,
    Capability,
    ProbeResult,
    ProbeStatus,
    StreamEndpoint,
)

_DOCS = "https://github.com/baadev/vidette/blob/main/docs/cameras/eufy.md"
_NOT_READY = (
    f"the eufy adapter is designed for M2 and not functional yet — see {_DOCS} and ROADMAP.md. "
    "If your Eufy model supports native RTSP, use `adapter: rtsp` today instead."
)


class EufyOptions(BaseModel):
    """Adapter-specific `options:` — validated here, documented in docs/cameras/eufy.md."""

    model_config = ConfigDict(extra="forbid")

    bridge_url: str = "ws://eufy-ws:3000"
    station_sn: str | None = None


class EufyAdapter:
    info = AdapterInfo(
        id="eufy",
        display_name="Eufy (via eufy-security-ws bridge)",
        maturity="designed",
        capabilities=(
            Capability.LIVE_MAIN
            | Capability.EVENTS_PUSH
            | Capability.SNAPSHOT
            | Capability.CLIP_DOWNLOAD
        ),
        docs_url=_DOCS,
    )

    async def probe(self, camera_id: str, config: CameraConfig) -> ProbeResult:
        try:
            EufyOptions.model_validate(config.options)
        except ValidationError as exc:
            first = exc.errors()[0]
            return ProbeResult(
                ProbeStatus.misconfigured,
                f"camera '{camera_id}': invalid eufy options — {first['loc']}: {first['msg']} "
                f"(see {_DOCS})",
            )
        return ProbeResult(ProbeStatus.not_implemented, f"camera '{camera_id}': {_NOT_READY}")

    async def stream_endpoints(self, camera_id: str, config: CameraConfig) -> list[StreamEndpoint]:
        raise AdapterNotReadyError(_NOT_READY)

    async def observations(
        self, camera_id: str, config: CameraConfig
    ) -> AsyncIterator[Observation]:
        nothing: tuple[Observation, ...] = ()
        for observation in nothing:  # empty async generator until the M2 bridge lands
            yield observation
