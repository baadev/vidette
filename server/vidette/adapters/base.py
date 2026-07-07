"""Camera adapter SDK — the typed contract every ecosystem implements.

Design (docs/architecture/plugins.md): an adapter answers four questions — who are you
(AdapterInfo + capability flags), can you reach this camera (probe → *actionable*
diagnostics), where are the streams (endpoints handed to go2rtc — adapters never decode),
and what is happening (vendor push events normalized to Observations).

Third-party adapters register via the `vidette.adapters` entry-point group; non-Python
ecosystem clients are wrapped as sidecar containers with a thin adapter here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Flag, StrEnum, auto
from importlib.metadata import entry_points
from typing import Literal, Protocol

from pydantic import BaseModel

from vidette.core.config import CameraConfig
from vidette.core.events import Observation


class AdapterError(Exception):
    """Adapter failure with a user-actionable message."""


class AdapterNotReadyError(AdapterError):
    """The adapter is designed but not yet functional; message points at docs + milestone."""


class Capability(Flag):
    NONE = 0
    LIVE_MAIN = auto()
    LIVE_SUB = auto()
    EVENTS_PUSH = auto()
    SNAPSHOT = auto()
    CLIP_DOWNLOAD = auto()
    PTZ = auto()
    TWO_WAY_AUDIO = auto()


Maturity = Literal["stable", "beta", "designed"]


@dataclass(frozen=True)
class AdapterInfo:
    id: str
    display_name: str
    maturity: Maturity  # rendered in the UI; the M5 conformance suite verifies honesty
    capabilities: Capability
    docs_url: str


class ProbeStatus(StrEnum):
    ok = "ok"
    unreachable = "unreachable"
    auth_failed = "auth_failed"
    misconfigured = "misconfigured"
    not_implemented = "not_implemented"


@dataclass(frozen=True)
class ProbeResult:
    status: ProbeStatus
    detail: str  # must tell the user what to do next — "auth failed" ≠ "host unreachable"

    @property
    def ok(self) -> bool:
        return self.status is ProbeStatus.ok


class StreamEndpoint(BaseModel):
    role: Literal["main", "sub"]
    url: str  # consumed by go2rtc; adapters never decode video themselves


class CameraAdapter(Protocol):
    """The contract. Implementations should be stateless per-call at this stage."""

    info: AdapterInfo

    async def probe(self, camera_id: str, config: CameraConfig) -> ProbeResult:
        """Cheap diagnostic: can this configuration plausibly work, and if not, why exactly."""
        ...

    async def stream_endpoints(self, camera_id: str, config: CameraConfig) -> list[StreamEndpoint]:
        """Endpoints to feed the stream gateway. Raise AdapterNotReadyError if designed-only."""
        ...

    def observations(self, camera_id: str, config: CameraConfig) -> AsyncIterator[Observation]:
        """Vendor push events (motion/doorbell/battery) normalized as Observations."""
        ...


def builtin_adapters() -> dict[str, CameraAdapter]:
    # Imported lazily to avoid import cycles at module load.
    from vidette.adapters.eufy import EufyAdapter
    from vidette.adapters.rtsp import RtspAdapter

    adapters: list[CameraAdapter] = [RtspAdapter(), EufyAdapter()]
    return {adapter.info.id: adapter for adapter in adapters}


def available_adapters() -> dict[str, CameraAdapter]:
    """Builtin adapters + any installed via the `vidette.adapters` entry-point group."""
    registry = builtin_adapters()
    for entry in entry_points(group="vidette.adapters"):
        if entry.name in registry:
            continue  # builtins win over shadowing; third parties must pick unique ids
        try:
            adapter_cls = entry.load()
            adapter: CameraAdapter = adapter_cls()
        except Exception as exc:  # a broken plugin must not take the core down
            raise AdapterError(
                f"adapter entry point '{entry.name}' failed to load: {exc}"
            ) from exc
        registry[adapter.info.id] = adapter
    return registry
