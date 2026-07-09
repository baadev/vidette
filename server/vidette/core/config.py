"""Configuration schema and loader.

This module *is* the configuration reference: `docs/configuration.md` mirrors it, and a test
keeps `deploy/config.example.yaml` valid against it. Two honesty rules are enforced here:

- secrets never live in YAML — `${VAR}` references are interpolated strictly (unset = error);
- configured-but-not-yet-implemented features produce explicit warnings with their milestone,
  so the config never silently lies about what will happen (see `designed_feature_warnings`).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    ValidationError,
    model_validator,
)


class ConfigError(Exception):
    """Configuration cannot be loaded; the message says what to fix."""


# --- durations -------------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_DURATION_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def parse_duration(value: object) -> timedelta | None:
    """Parse '90s' / '30m' / '12h' / '3d' / 'forever' → timedelta (None means forever)."""
    if value is None or isinstance(value, timedelta):
        return value
    if isinstance(value, str):
        if value == "forever":
            return None
        match = _DURATION_RE.match(value)
        if match:
            amount, unit = match.groups()
            return timedelta(**{_DURATION_UNITS[unit]: int(amount)})
    raise ValueError(
        f"invalid duration {value!r} — use e.g. '90s', '30m', '12h', '3d' or 'forever'"
    )


def format_duration(value: timedelta | None) -> str:
    """Inverse of parse_duration — keeps model_dump(mode="json") round-trippable
    (pydantic's default ISO-8601 'P3D' would be rejected on the way back in)."""
    if value is None:
        return "forever"
    total = int(value.total_seconds())
    for unit_seconds, suffix in ((86400, "d"), (3600, "h"), (60, "m")):
        if total >= unit_seconds and total % unit_seconds == 0:
            return f"{total // unit_seconds}{suffix}"
    return f"{total}s"


Duration = Annotated[
    timedelta | None,
    BeforeValidator(parse_duration),
    PlainSerializer(format_duration, when_used="json"),
]


# --- building blocks -------------------------------------------------------------------------

class StrictModel(BaseModel):
    """Reject unknown keys: a typo in the config must be an error, not a silent no-op."""

    model_config = ConfigDict(extra="forbid")


class AuthMode(StrEnum):
    builtin = "builtin"
    none = "none"


class AuthConfig(StrictModel):
    mode: AuthMode = AuthMode.builtin


class ServerConfig(StrictModel):
    host: str = "0.0.0.0"  # container default; exposure guidance in security-model.md
    port: int = Field(default=8642, ge=1, le=65535)
    base_url: str | None = None
    auth: AuthConfig = AuthConfig()
    # ICE candidates advertised by the WebRTC gateway, e.g. ["192.168.10.20:8555"].
    # Inside a container go2rtc only knows its bridge/STUN addresses, which LAN browsers
    # cannot reach — set your host's LAN IP here (or VIDETTE_WEBRTC_CANDIDATES) for direct
    # WebRTC. Live view works without it: the player falls back to MSE over WebSocket.
    webrtc_candidates: list[str] = Field(default_factory=list)


class Retention(StrictModel):
    continuous: Duration = timedelta(days=3)
    motion: Duration = timedelta(days=14)
    events: Duration = timedelta(days=90)
    favorites: Duration = None  # forever


class CompactionConfig(StrictModel):
    enabled: bool = False
    after: Duration = timedelta(days=7)
    codec: Literal["hevc", "av1"] = "hevc"


class OffsiteConfig(StrictModel):
    enabled: bool = False


class StorageConfig(StrictModel):
    media_dir: Path = Path("/media/vidette")
    database: Path = Path("/config/vidette.db")
    retention: Retention = Retention()
    compaction: CompactionConfig = CompactionConfig()
    offsite: OffsiteConfig = OffsiteConfig()


class ZoneKind(StrEnum):
    entry = "entry"      # doors, gates, windows — approach/dwell/touch is high-signal
    object = "object"    # protected things: wall equipment, bike, car
    private = "private"  # yard, porch — presence notable, transit not
    public = "public"    # street, sidewalk — tracks that never leave it are suppressed


class Zone(StrictModel):
    kind: ZoneKind
    points: list[tuple[float, float]] = Field(min_length=3)

    @model_validator(mode="after")
    def _points_normalized(self) -> Zone:
        for x, y in self.points:
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise ValueError(
                    f"zone points must be normalized to 0.0–1.0, got ({x}, {y})"
                )
        return self


class RecordMode(StrEnum):
    continuous = "continuous"
    motion = "motion"
    events = "events"
    off = "off"


class RecordConfig(StrictModel):
    mode: RecordMode = RecordMode.continuous
    retention: Retention | None = None  # None → global storage.retention


class DetectConfig(StrictModel):
    enabled: bool = True
    fps: float = Field(default=5.0, gt=0, le=30)
    resolution: int = Field(default=720, ge=240, le=2160)


class CameraSource(StrictModel):
    main: str
    sub: str | None = None


class CameraConfig(StrictModel):
    adapter: str = "rtsp"
    name: str | None = None
    source: CameraSource | None = None
    options: dict[str, Any] = Field(default_factory=dict)  # adapter-specific, validated by adapter
    record: RecordConfig = RecordConfig()
    detect: DetectConfig = DetectConfig()
    understand: bool = True
    zones: dict[str, Zone] = Field(default_factory=dict)


class Hardware(StrEnum):
    auto = "auto"
    cpu = "cpu"
    cuda = "cuda"
    openvino = "openvino"
    coreml = "coreml"
    hailo = "hailo"
    coral = "coral"


class DetectorConfig(StrictModel):
    model: str = "auto"
    hardware: Hardware = Hardware.auto


class TrackerConfig(StrictModel):
    engine: Literal["bytetrack"] = "bytetrack"


class VlmProvider(StrEnum):
    none = "none"
    ollama = "ollama"
    llama_cpp = "llama-cpp"
    openai = "openai"
    anthropic = "anthropic"
    google = "google"


class VlmConfig(StrictModel):
    provider: VlmProvider = VlmProvider.none
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None  # reference it as ${PROVIDER_KEY}; never a literal
    max_calls_per_minute: int = Field(default=6, ge=1)
    send: Literal["keyframes", "crops"] = "keyframes"


class EmbeddingsConfig(StrictModel):
    enabled: bool = False
    model: str = "siglip2-base"


class FacesConfig(StrictModel):
    """Trusted-faces suppression (M4) — opt-in, local-only; guardrails in docs/faq.md.

    Enrollment (photos → local embeddings) happens in the UI; biometrics never live in YAML.
    An uncertain match never suppresses — the system fails toward alerting.
    """

    enabled: bool = False
    min_confidence: float = Field(default=0.8, ge=0.5, le=1.0)


class UnderstandingConfig(StrictModel):
    detector: DetectorConfig = DetectorConfig()
    tracker: TrackerConfig = TrackerConfig()
    vlm: VlmConfig = VlmConfig()
    faces: FacesConfig = FacesConfig()
    embeddings: EmbeddingsConfig = EmbeddingsConfig()


class Sensitivity(StrEnum):
    relaxed = "relaxed"
    balanced = "balanced"
    paranoid = "paranoid"


class PolicyConfig(StrictModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str
    cameras: list[str] | Literal["all"] = "all"
    sensitivity: Sensitivity = Sensitivity.balanced
    # Skip alerts for tracks confidently matched to trusted faces (understanding.faces, M4).
    ignore_trusted: bool = True
    actions: list[str] = Field(default_factory=lambda: ["notify"])


class ChannelKind(StrEnum):
    webpush = "webpush"
    apprise = "apprise"
    webhook = "webhook"


class ChannelConfig(StrictModel):
    kind: ChannelKind
    enabled: bool = True
    url: str | None = None
    secret: str | None = None
    include: list[str] = Field(default_factory=lambda: ["summary", "snapshot_url", "clip_url"])

    @model_validator(mode="after")
    def _required_fields(self) -> ChannelConfig:
        if self.kind in (ChannelKind.webhook, ChannelKind.apprise) and not self.url:
            raise ValueError(f"channel kind '{self.kind.value}' requires 'url'")
        if self.kind is ChannelKind.webhook and not self.secret:
            raise ValueError(
                "webhook channels require 'secret' (use ${ENV_VAR}) — unsigned webhooks are "
                "spoofable; see docs/events-and-automations.md"
            )
        return self


class NotifyRule(StrictModel):
    # Named `when` (not `on`): YAML 1.1 parses a bare `on` key as boolean True — a footgun
    # we refuse to ship to users.
    when: str = "event.confirmed"  # exact type, or a pattern like 'event.*' / 'system.*'
    channels: list[str] = Field(min_length=1)


class NotificationsConfig(StrictModel):
    channels: dict[str, ChannelConfig] = Field(default_factory=dict)
    rules: list[NotifyRule] = Field(default_factory=list)


class MqttConfig(StrictModel):
    enabled: bool = False
    host: str | None = None
    port: int = Field(default=1883, ge=1, le=65535)
    username: str | None = None
    password: str | None = None
    topic_prefix: str = "vidette"
    discovery: bool = True  # Home Assistant MQTT discovery

    @model_validator(mode="after")
    def _host_if_enabled(self) -> MqttConfig:
        if self.enabled and not self.host:
            raise ValueError("integrations.mqtt.enabled requires 'host'")
        return self


class IntegrationsConfig(StrictModel):
    mqtt: MqttConfig = MqttConfig()


class TelemetryConfig(StrictModel):
    # Nothing is sent today either way; the key exists so the promise stays visible.
    enabled: bool = False


_CAMERA_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class VidetteConfig(StrictModel):
    server: ServerConfig = ServerConfig()
    storage: StorageConfig = StorageConfig()
    cameras: dict[str, CameraConfig] = Field(default_factory=dict)
    understanding: UnderstandingConfig = UnderstandingConfig()
    policies: list[PolicyConfig] = Field(default_factory=list)
    notifications: NotificationsConfig = NotificationsConfig()
    integrations: IntegrationsConfig = IntegrationsConfig()
    telemetry: TelemetryConfig = TelemetryConfig()

    @model_validator(mode="after")
    def _cross_references(self) -> VidetteConfig:
        for camera_id in self.cameras:
            if not _CAMERA_ID_RE.match(camera_id):
                raise ValueError(
                    f"camera id '{camera_id}' must match [a-z0-9-] (it is used in URLs and topics)"
                )
        for policy in self.policies:
            if policy.cameras == "all":
                continue
            for camera_id in policy.cameras:
                if camera_id not in self.cameras:
                    raise ValueError(
                        f"policy '{policy.name}' references unknown camera '{camera_id}'"
                    )
        for rule in self.notifications.rules:
            for channel in rule.channels:
                if channel not in self.notifications.channels:
                    raise ValueError(
                        f"notification rule '{rule.when}' references unknown channel '{channel}'"
                    )
        return self


# --- honesty: designed-but-not-implemented warnings -------------------------------------------

def designed_feature_warnings(config: VidetteConfig) -> list[str]:
    """One warning per configured feature that is still design-stage, with its milestone.

    Keep in sync with ROADMAP.md — the point is that `vidette validate` never lets a config
    silently pretend. Remove entries as milestones ship.
    """
    warnings: list[str] = []
    roadmap = "see ROADMAP.md"
    if not config.cameras:
        warnings.append("cameras: none configured — Vidette will start, but watch nothing")
    for camera_id, camera in config.cameras.items():
        if camera.record.mode in (RecordMode.motion, RecordMode.events):
            warnings.append(
                f"cameras.{camera_id}: record.mode '{camera.record.mode.value}' falls back "
                f"to continuous until detection lands in M2 ({roadmap}) — recording more "
                "than asked, never less"
            )
        if camera.adapter == "eufy":
            # The most likely misconfiguration from Eufy owners: there is no bridge adapter
            # (Anker shut down the legacy API the community client relied on).
            warnings.append(
                f"cameras.{camera_id}: there is no 'eufy' adapter — Eufy cameras connect "
                "through their built-in NAS (RTSP) feature using `adapter: rtsp`, and only "
                "on models that support it; see docs/cameras/eufy.md"
            )
    if config.server.auth.mode is AuthMode.none:
        warnings.append(
            "server.auth.mode=none disables authentication — acceptable only on a trusted "
            "LAN/kiosk; a permanent UI banner will remind you"
        )
    if config.storage.compaction.enabled:
        warnings.append(f"storage.compaction: designed — lands in M3 ({roadmap})")
    if config.storage.offsite.enabled:
        warnings.append(f"storage.offsite: designed — lands in M3 ({roadmap})")
    if config.understanding.vlm.provider is not VlmProvider.none:
        warnings.append(f"understanding.vlm: designed — lands in M3 ({roadmap})")
    if config.understanding.faces.enabled:
        warnings.append(
            f"understanding.faces (trusted-faces suppression): designed — lands in M4 ({roadmap})"
        )
    if config.understanding.embeddings.enabled:
        warnings.append(
            f"understanding.embeddings (semantic search): designed — lands in M3 ({roadmap})"
        )
    if config.policies:
        warnings.append(
            "policies: plain-language interpretation lands in M4 — today each policy "
            "applies its geometric skeleton (zones + sensitivity presets)"
        )
    return warnings


# --- loading ----------------------------------------------------------------------------------

_ENV_REF_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def _interpolate_node(node: object, env: Mapping[str, str], missing: set[str]) -> object:
    """Replace ${VAR} inside string *values* of the parsed tree (never inside comments/keys)."""
    if isinstance(node, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in env:
                missing.add(name)
                return match.group(0)
            return env[name]

        return _ENV_REF_RE.sub(replace, node)
    if isinstance(node, dict):
        return {key: _interpolate_node(value, env, missing) for key, value in node.items()}
    if isinstance(node, list):
        return [_interpolate_node(item, env, missing) for item in node]
    return node


def _format_validation_error(error: ValidationError) -> list[str]:
    messages = []
    for issue in error.errors():
        location = ".".join(str(part) for part in issue["loc"]) or "<root>"
        messages.append(f"{location}: {issue['msg']}")
    return messages


class ValidationReport(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _load_from_text(
    text: str, env: Mapping[str, str] | None
) -> tuple[VidetteConfig | None, ValidationReport]:
    resolved_env: Mapping[str, str] = os.environ if env is None else env
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, ValidationReport(valid=False, errors=[f"not valid YAML: {exc}"])
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return None, ValidationReport(valid=False, errors=["top level must be a mapping"])

    missing: set[str] = set()
    interpolated = _interpolate_node(raw, resolved_env, missing)
    if missing:
        return None, ValidationReport(
            valid=False,
            errors=[
                "missing environment variables referenced in config: "
                + ", ".join(sorted(missing))
                + " — set them in the container environment or an .env file"
            ],
        )
    try:
        config = VidetteConfig.model_validate(interpolated)
    except ValidationError as exc:
        return None, ValidationReport(valid=False, errors=_format_validation_error(exc))
    return config, ValidationReport(valid=True, warnings=designed_feature_warnings(config))


def validate_config_text(text: str, env: Mapping[str, str] | None = None) -> ValidationReport:
    """Validate raw YAML text; never raises — returns a report suitable for CLI/API display."""
    return _load_from_text(text, env)[1]


def load_config(
    path: Path, env: Mapping[str, str] | None = None
) -> tuple[VidetteConfig, list[str]]:
    """Load and validate a config file; raises ConfigError with actionable messages."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    config, report = _load_from_text(text, env)
    if config is None:
        raise ConfigError(f"config {path} is invalid:\n  - " + "\n  - ".join(report.errors))
    return config, report.warnings
