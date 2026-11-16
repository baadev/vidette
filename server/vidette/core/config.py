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
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)


class ConfigError(Exception):
    """Configuration cannot be loaded; the message says what to fix."""


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


class StorageConfig(StrictModel):
    # Vidette records nothing (ADR-0009): media_dir only caches what it fetched from
    # Frigate for its own events (snapshots, judged frames, clips on demand).
    media_dir: Path = Path("/media/vidette")
    database: Path = Path("/config/vidette.db")


# Frigate camera names are its config keys: letters, digits, '_' and '-'.
_FRIGATE_CAMERA_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _check_camera_names(names: list[str], where: str) -> None:
    for name in names:
        if not _FRIGATE_CAMERA_RE.match(name):
            raise ValueError(
                f"{where}: camera '{name}' is not a valid Frigate camera name; use the "
                "camera's key from Frigate's config (letters, digits, '_' and '-'), not its "
                "friendly_name"
            )


class FrigateMqttConfig(StrictModel):
    """The broker Frigate publishes to (Frigate's own `mqtt:` section)."""

    host: str
    port: int = Field(default=1883, ge=1, le=65535)
    username: str | None = None
    password: str | None = None  # reference it as ${FRIGATE_MQTT_PASSWORD}
    topic_prefix: str = "frigate"  # Frigate's mqtt.topic_prefix


class Severity(StrEnum):
    alert = "alert"
    detection = "detection"


class FrigateConfig(StrictModel):
    """Where Vidette reads incidents from (docs/frigate.md).

    `url` is Frigate's HTTP API base, normally the internal unauthenticated port on the
    compose network (`http://frigate:5000`). The authenticated port (8971) is not supported
    yet.
    """

    url: str
    mqtt: FrigateMqttConfig
    cameras: list[str] | Literal["all"] = "all"
    # Review severities that become incidents. Frigate marks a review `alert` when its
    # alert rules match (by default person/car in any zone) and `detection` otherwise.
    severities: list[Severity] = Field(
        default_factory=lambda: [Severity.alert, Severity.detection], min_length=1
    )
    # Also subscribe to <prefix>/events (per tracked object: zones, stationary, scores).
    tracked_objects: bool = True

    @model_validator(mode="after")
    def _check(self) -> FrigateConfig:
        if not self.url.startswith(("http://", "https://")):
            raise ValueError(
                f"frigate.url '{self.url}' must start with http:// or https://, e.g. "
                "http://frigate:5000"
            )
        self.url = self.url.rstrip("/")
        if self.cameras != "all":
            _check_camera_names(self.cameras, "frigate.cameras")
        return self


def _duration(value: str, where: str) -> float:
    from vidette.units import parse_duration

    try:
        return parse_duration(value)
    except ValueError as exc:
        raise ValueError(f"{where}: {exc}") from None


class ClipFolderConfig(StrictModel):
    """A folder of finished clips, read as incidents without Frigate (ADR-0010).

    For cameras that cannot feed Frigate: a `vidette collect` dataset, or a folder a
    camera uploads its motion clips to (FTP, SMB). One clip becomes one incident; a
    `vidette collect` JSON sidecar next to it supplies the camera, times and key frames.
    """

    path: Path
    camera: str = "camera"  # event camera name when a clip has no sidecar
    poll_every: str = "10s"
    settle: str = "5s"  # a file must be unchanged this long (uploads in progress)
    backfill: bool = False  # also turn clips that were there before the first start

    @model_validator(mode="after")
    def _check(self) -> ClipFolderConfig:
        _check_camera_names([self.camera], "clip_folders.camera")
        if _duration(self.poll_every, "clip_folders.poll_every") < 1:
            raise ValueError("clip_folders.poll_every must be at least 1s")
        _duration(self.settle, "clip_folders.settle")
        return self

    @property
    def poll_every_s(self) -> float:
        return _duration(self.poll_every, "poll_every")

    @property
    def settle_s(self) -> float:
        return _duration(self.settle, "settle")


class VlmBackend(StrEnum):
    """Which model judges: none, the local one, or the cloud one (the user chooses)."""

    none = "none"
    local = "local"
    cloud = "cloud"


class CloudPreset(StrEnum):
    openai = "openai"
    gemini = "gemini"  # Google Gemini's OpenAI-compatible endpoint
    openrouter = "openrouter"
    custom = "custom"  # any other OpenAI-compatible API: set base_url


@dataclass(frozen=True)
class PresetDefaults:
    base_url: str
    model: str
    vendor: str  # who receives the frames, for the privacy line in the UI
    token_param: str = "max_tokens"  # OpenAI's newer models want max_completion_tokens


OLLAMA_URL = "http://localhost:11434/v1"

# Model names are suggestions that existed when this was written; check your provider's
# current list. Prices are never defaulted: a cost of $0 is printed as "price not set".
CLOUD_PRESETS: dict[CloudPreset, PresetDefaults] = {
    CloudPreset.openai: PresetDefaults(
        "https://api.openai.com/v1", "gpt-4.1-mini", "OpenAI", "max_completion_tokens"
    ),
    CloudPreset.gemini: PresetDefaults(
        "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash", "Google"
    ),
    CloudPreset.openrouter: PresetDefaults(
        "https://openrouter.ai/api/v1", "google/gemini-2.5-flash", "OpenRouter"
    ),
}


class VlmPrices(StrictModel):
    """What one call costs, to report money next to accuracy. All default to 0."""

    usd_per_mtok_in: float = Field(default=0.0, ge=0)  # per million input tokens
    usd_per_mtok_out: float = Field(default=0.0, ge=0)
    usd_per_image: float = Field(default=0.0, ge=0)  # for APIs that bill per image
    usd_per_call: float = Field(default=0.0, ge=0)

    @property
    def is_set(self) -> bool:
        return any(
            (self.usd_per_mtok_in, self.usd_per_mtok_out, self.usd_per_image, self.usd_per_call)
        )


class LocalVlmConfig(StrictModel):
    """An OpenAI-compatible server on your own hardware; Ollama by default."""

    base_url: str = OLLAMA_URL  # in Docker: http://host.docker.internal:11434/v1
    model: str = "qwen2.5vl:7b"
    api_key: str | None = None  # Ollama needs none; llama.cpp/vLLM may
    timeout_s: float = Field(default=120.0, gt=0)  # CPU inference with 4 images is slow
    max_calls_per_day: int = Field(default=2000, ge=1)
    max_calls_per_minute: int = Field(default=20, ge=1)
    max_output_tokens: int = Field(default=400, ge=50)
    max_image_side: int = Field(default=768, ge=128, le=2048)
    prices: VlmPrices = VlmPrices()

    @model_validator(mode="after")
    def _check(self) -> LocalVlmConfig:
        _check_http_url(self.base_url, "understanding.vlm.local.base_url")
        self.base_url = self.base_url.rstrip("/")
        return self


class CloudVlmConfig(StrictModel):
    """A hosted OpenAI-compatible API. Choosing it sends camera frames to that vendor."""

    preset: CloudPreset = CloudPreset.openai
    base_url: str | None = None  # default from the preset; required for custom
    model: str | None = None  # default from the preset
    api_key: str  # ${OPENAI_API_KEY}, ${GEMINI_API_KEY}, ${OPENROUTER_API_KEY}; never a literal
    timeout_s: float = Field(default=45.0, gt=0)
    max_calls_per_day: int = Field(default=200, ge=1)
    max_calls_per_minute: int = Field(default=10, ge=1)
    max_output_tokens: int = Field(default=400, ge=50)
    max_image_side: int = Field(default=768, ge=128, le=2048)
    prices: VlmPrices = VlmPrices()

    @model_validator(mode="after")
    def _check(self) -> CloudVlmConfig:
        if self.preset is CloudPreset.custom and not self.base_url:
            raise ValueError("understanding.vlm.cloud: preset custom requires base_url")
        if self.base_url is not None:
            _check_http_url(self.base_url, "understanding.vlm.cloud.base_url")
            self.base_url = self.base_url.rstrip("/")
        if not self.api_key.strip():
            raise ValueError("understanding.vlm.cloud.api_key is empty")
        return self

    @property
    def resolved_base_url(self) -> str:
        if self.base_url:
            return self.base_url
        return CLOUD_PRESETS[self.preset].base_url

    @property
    def resolved_model(self) -> str:
        if self.model:
            return self.model
        if self.preset is CloudPreset.custom:
            raise ValueError("understanding.vlm.cloud: preset custom requires model")
        return CLOUD_PRESETS[self.preset].model

    @property
    def vendor(self) -> str:
        if self.preset is CloudPreset.custom:
            return "a custom provider"
        return CLOUD_PRESETS[self.preset].vendor

    @property
    def token_param(self) -> str:
        if self.preset is CloudPreset.custom:
            return "max_tokens"
        return CLOUD_PRESETS[self.preset].token_param


def _check_http_url(url: str, where: str) -> None:
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"{where} '{url}' must start with http:// or https://")


class VlmConfig(StrictModel):
    """The VLM judge (docs/vlm.md): one OpenAI-compatible client, two backends.

    `backend` is the default choice; the System page can switch it, and that choice is
    stored in the database and wins until changed again.
    """

    backend: VlmBackend = VlmBackend.none
    frames_per_call: int = Field(default=4, ge=1, le=8)
    # The language of the judge's one-line description (what notifications quote); the
    # rest of the prompt stays English. None: the model's default, English.
    language: str | None = Field(default=None, max_length=40)
    local: LocalVlmConfig = LocalVlmConfig()
    cloud: CloudVlmConfig | None = None

    @model_validator(mode="before")
    @classmethod
    def _v01_keys(cls, data: object) -> object:
        if isinstance(data, dict) and ("provider" in data or "send" in data):
            raise ValueError(
                "understanding.vlm.provider/send were replaced in v0.2: use backend: "
                "none | local | cloud, with a local: (Ollama) and/or cloud: section "
                "(docs/vlm.md)"
            )
        return data

    @model_validator(mode="after")
    def _check(self) -> VlmConfig:
        if self.backend is VlmBackend.cloud and self.cloud is None:
            raise ValueError(
                "understanding.vlm.backend is cloud but there is no cloud: section; add one "
                "with preset, model and api_key (docs/vlm.md)"
            )
        if self.cloud is not None:
            self.cloud.resolved_model  # noqa: B018 - raises for custom without a model
        return self


_WINDOW_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")


class DeepConfig(StrictModel):
    """Deep analysis (docs/deep.md, ADR-0019): a slower second look at the day's clips
    with people or vehicles, on the judge's own endpoint, one request at a time, never
    replacing the live verdict. Disagreements go to the next labelling batch; the evening
    digest uses its descriptions."""

    enabled: bool = False
    backend: Literal["local", "cloud"] = "local"  # which understanding.vlm endpoint
    window: str = "01:00-07:00"  # local time; may wrap midnight ("22:00-06:00")
    outside_window: Literal["trickle", "off"] = "trickle"
    trickle_every: str = "10m"  # outside the window: at most one clip this often
    max_clips_per_day: int = Field(default=150, ge=1, le=10000)
    lookback: str = "36h"  # events this recent are considered
    frames: int = Field(default=8, ge=1, le=24)  # per clip, shown `frames_per_call` at a time
    frames_per_call: int | None = Field(default=None, ge=1, le=8)  # default: the judge's
    max_output_tokens: int = Field(default=600, ge=100, le=8000)
    language: str | None = Field(default=None, max_length=40)  # default: the judge's
    include_unjudged: bool = True  # events the live judge never reached (lowest priority)
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)  # below: uncertain
    # Before every request, ask the server's /slots (llama.cpp) and send only when at least
    # this many slots are idle; `ignore` for servers without it (Ollama, cloud).
    slots: Literal["check", "ignore"] = "check"
    min_idle_slots: int = Field(default=1, ge=1, le=64)
    max_attempts: int = Field(default=3, ge=1, le=10)
    label_disputes: bool = True  # disputed and uncertain clips become review drafts

    @model_validator(mode="after")
    def _check(self) -> DeepConfig:
        if not _WINDOW_RE.match(self.window):
            raise ValueError(
                f"understanding.deep.window '{self.window}' must look like 01:00-07:00"
            )
        _duration(self.trickle_every, "understanding.deep.trickle_every")
        if _duration(self.lookback, "understanding.deep.lookback") < 3600:
            raise ValueError("understanding.deep.lookback must be at least 1h")
        return self

    @property
    def window_minutes(self) -> tuple[int, int]:
        match = _WINDOW_RE.match(self.window)
        assert match is not None
        h1, m1, h2, m2 = (int(g) for g in match.groups())
        return h1 * 60 + m1, h2 * 60 + m2

    def in_window(self, minute_of_day: int) -> bool:
        start, end = self.window_minutes
        if start == end:
            return True
        if start < end:
            return start <= minute_of_day < end
        return minute_of_day >= start or minute_of_day < end

    @property
    def trickle_every_s(self) -> float:
        return _duration(self.trickle_every, "trickle_every")

    @property
    def lookback_s(self) -> float:
        return _duration(self.lookback, "lookback")


class UnderstandingConfig(StrictModel):
    vlm: VlmConfig = VlmConfig()
    deep: DeepConfig = Field(default_factory=DeepConfig)

    @model_validator(mode="after")
    def _deep_backend(self) -> UnderstandingConfig:
        if self.deep.enabled and self.deep.backend == "cloud" and self.vlm.cloud is None:
            raise ValueError(
                "understanding.deep.backend is cloud but understanding.vlm has no cloud: section"
            )
        return self


class Sensitivity(StrEnum):
    relaxed = "relaxed"
    balanced = "balanced"
    paranoid = "paranoid"


class Wake(StrEnum):
    """Who gets woken for a verdict at a level (docs/telegram.md#levels)."""

    always = "always"  # immediately, also in quiet hours and when only known people are seen
    unless_quiet = "unless_quiet"  # immediately, except in quiet hours (then the digest)
    unless_known = "unless_known"  # immediately, unless only known people are seen or quiet
    never = "never"  # not an alert: the digest only (vocabulary for what is ordinary)


OTHER_SCENARIO = "other"  # the judge's catch-all: none of a policy's scenarios (never alerts)
_RESERVED_SCENARIOS = frozenset({OTHER_SCENARIO, "unsure", "unclear", "unlabelled"})
_SCENARIO_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class ScenarioConfig(StrictModel):
    description: str = Field(min_length=1)  # what the judge is told this scenario means
    title: str | None = Field(default=None, max_length=40)  # short name in digests


class LevelConfig(StrictModel):
    """One severity level of a policy: the scenarios that belong to it and who wakes."""

    title: str | None = Field(default=None, max_length=40)  # e.g. "critical", shown in alerts
    wake: Wake = Wake.unless_quiet
    scenarios: dict[str, str | ScenarioConfig] = Field(min_length=1)

    def scenario_configs(self) -> dict[str, ScenarioConfig]:
        return {
            key: value if isinstance(value, ScenarioConfig) else ScenarioConfig(description=value)
            for key, value in self.scenarios.items()
        }


class RecurrenceConfig(StrictModel):
    """The same unknown face coming back raises a verdict to `raise_to` (ADR-0014).

    Needs `people.track_unknown`. Only a verdict that named a scenario of a level below
    `raise_to` is raised; `other`, an abstention and a verdict already at or above it
    stay as the judge said. Behaviour over time only: how often and where."""

    raise_to: str  # a level of this policy
    sightings: int = Field(default=3, ge=2, le=100)  # the same face this many times...
    within: str = "24h"  # ...within this window, this sighting included
    cameras: int | None = Field(default=2, ge=2, le=20)  # or on this many cameras...
    cameras_within: str = "10m"  # ...within this window; `cameras: null` turns it off
    suspicious: bool = True  # an identity marked suspicious raises on every sighting

    @model_validator(mode="after")
    def _check(self) -> RecurrenceConfig:
        _duration(self.within, "recurrence.within")
        _duration(self.cameras_within, "recurrence.cameras_within")
        return self

    @property
    def within_s(self) -> float:
        return _duration(self.within, "within")

    @property
    def cameras_within_s(self) -> float:
        return _duration(self.cameras_within, "cameras_within")


class PolicyConfig(StrictModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str
    cameras: list[str] | Literal["all"] = "all"  # Frigate camera names
    sensitivity: Sensitivity = Sensitivity.balanced
    actions: list[str] = Field(default_factory=lambda: ["notify"])
    # With people: enabled, "quiet" tells the judge that people recognized as known never
    # need an alert by themselves; "judge" only reports who was seen (docs/people.md).
    known_people: Literal["judge", "quiet"] = "judge"
    # Levels (docs/telegram.md#levels): the judge picks one scenario, the scenario's level
    # decides whether it is an alert and who wakes. Highest level first. Without levels the
    # judge answers alert / silent, and an alert wakes like `unless_quiet`.
    levels: dict[str, LevelConfig] = Field(default_factory=dict)
    # Repeated presence of the same unknown face raises the level (docs/people.md, ADR-0014)
    recurrence: RecurrenceConfig | None = None

    @model_validator(mode="after")
    def _check_recurrence(self) -> PolicyConfig:
        if self.recurrence is not None and self.recurrence.raise_to not in self.levels:
            names = ", ".join(self.levels) or "none: add levels first"
            raise ValueError(
                f"policy '{self.name}': recurrence.raise_to '{self.recurrence.raise_to}' is not "
                f"a level of this policy ({names})"
            )
        return self

    @model_validator(mode="after")
    def _check_levels(self) -> PolicyConfig:
        seen: set[str] = set()
        for level_name, level in self.levels.items():
            if not _SCENARIO_RE.match(level_name):
                raise ValueError(
                    f"policy '{self.name}': level '{level_name}' must be lower case letters, "
                    "digits and '_'"
                )
            for scenario in level.scenarios:
                if not _SCENARIO_RE.match(scenario):
                    raise ValueError(
                        f"policy '{self.name}': scenario '{scenario}' must be lower case "
                        "letters, digits and '_' (snake_case)"
                    )
                if scenario in _RESERVED_SCENARIOS:
                    raise ValueError(
                        f"policy '{self.name}': scenario '{scenario}' is reserved; "
                        f"'{OTHER_SCENARIO}' (none of the scenarios) is always there"
                    )
                if scenario in seen:
                    raise ValueError(
                        f"policy '{self.name}': scenario '{scenario}' is in two levels"
                    )
                seen.add(scenario)
        return self

    def scenario_level(self, scenario: str | None) -> tuple[str, LevelConfig] | None:
        """The level a scenario belongs to; None for `other`, unknown names, no levels."""
        for name, level in self.levels.items():
            if scenario in level.scenarios:
                return name, level
        return None

    def scenario_configs(self) -> dict[str, ScenarioConfig]:
        """Every scenario of every level, in level order."""
        merged: dict[str, ScenarioConfig] = {}
        for level in self.levels.values():
            merged.update(level.scenario_configs())
        return merged

    def wake_for(self, scenario: str | None) -> Wake:
        """How a verdict naming `scenario` wakes people (an alert, for a policy without
        levels, wakes `unless_quiet`)."""
        if not self.levels:
            return Wake.unless_quiet
        found = self.scenario_level(scenario)
        return Wake.never if found is None else found[1].wake


def _default_people_dir() -> Path:
    base = os.environ.get("VIDETTE_DATA")
    return (Path(base) if base else Path("~/vidette-data")).expanduser() / "people"


def _default_identities_dir() -> Path:
    base = os.environ.get("VIDETTE_DATA")
    return (Path(base) if base else Path("~/vidette-data")).expanduser() / "identities"


class IdentityMergeConfig(StrictModel):
    """The merge pass (ADR-0018): identities that are certainly one person, joined after
    the fact, every merge audited and reversible (`vidette people merge`)."""

    auto: bool = True  # vidette serve runs it after new sightings, at most every `every`
    every: str = "30m"
    clear: float = Field(default=0.5, ge=0.3, le=0.95)  # best clear-face cosine
    clear_floor: float = Field(default=0.4, ge=0.0, le=0.95)  # every clear pair at least
    margin: float = Field(default=0.06, ge=0.0, le=0.3)  # over the next candidate, both ways
    same_moment: float = Field(default=0.85, ge=0.5, le=1.0)  # any faces, one camera...
    same_moment_within: str = "10m"  # ...this close in time

    @model_validator(mode="after")
    def _check(self) -> IdentityMergeConfig:
        if _duration(self.every, "people.identities.merge.every") < 60:
            raise ValueError("people.identities.merge.every must be at least 1m")
        _duration(self.same_moment_within, "people.identities.merge.same_moment_within")
        if self.clear_floor > self.clear:
            raise ValueError("people.identities.merge.clear_floor cannot exceed clear")
        return self

    @property
    def every_s(self) -> float:
        return _duration(self.every, "every")

    @property
    def same_moment_within_s(self) -> float:
        return _duration(self.same_moment_within, "same_moment_within")


class IdentitiesConfig(StrictModel):
    """Anonymous identities for faces nobody enrolled (`people.track_unknown`, ADR-0014,
    ADR-0016): every person whose face the check found gets one.

    The folder holds face embeddings and crops of people who never agreed to anything:
    outside any git repository, owner-only, and pruned after `retention_days`."""

    dir: Path = Field(default_factory=_default_identities_dir)
    merge: IdentityMergeConfig = Field(default_factory=IdentityMergeConfig)
    match: float = Field(default=0.5, ge=0.3, le=0.95)  # SFace cosine to join an identity
    margin: float = Field(default=0.06, ge=0.0, le=0.3)  # over the next-closest identity
    # readable faces smaller than this start a weak identity (joined by hand only)
    min_face_px: int = Field(default=40, ge=16, le=400)
    near_known: float = Field(default=0.3, ge=0.0, le=0.95)  # probably them: never raises
    retention_days: int = Field(default=30, ge=1, le=3650)  # unnamed, unflagged ones

    @model_validator(mode="after")
    def _expand(self) -> IdentitiesConfig:
        self.dir = self.dir.expanduser()
        return self


class PeopleConfig(StrictModel):
    """Known people (docs/people.md, ADR-0012): local face recognition, opt-in.

    Needs `pip install 'vidette[people]'` and the two model files (`vidette people
    models`). The folder holds biometric data: it must be outside any git repository and
    is kept readable by its owner only."""

    enabled: bool = False
    dir: Path = Field(default_factory=_default_people_dir)
    models_dir: Path | None = None  # default ~/.cache/vidette/models
    threshold: float = Field(default=0.4, ge=0.2, le=0.95)  # SFace cosine similarity
    min_face_px: int = Field(default=40, ge=16, le=400)  # smaller faces are "unclear"
    frames_per_second: float = Field(default=2.0, gt=0, le=10)  # sampled from each clip
    max_frames: int = Field(default=24, ge=1, le=120)
    max_faces_per_person: int = Field(default=60, ge=5, le=500)
    # A second look around every face found, for its sharpest moment (ADR-0016): this many
    # frames a second, at most `extra_frames` more frames a clip (0 turns it off).
    extra_fps: float = Field(default=6.0, gt=0, le=30)
    extra_frames: int = Field(default=24, ge=0, le=240)
    # Anonymous identities for faces nobody enrolled (docs/people.md, ADR-0014): off by
    # default; biometric data of passers-by, local only, pruned after retention_days.
    track_unknown: bool = False
    identities: IdentitiesConfig = Field(default_factory=IdentitiesConfig)

    @model_validator(mode="after")
    def _expand(self) -> PeopleConfig:
        self.dir = self.dir.expanduser()
        if self.models_dir is not None:
            self.models_dir = self.models_dir.expanduser()
        return self


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


class TelegramTopic(StrictModel):
    """Forum topics (message_thread_id) of one chat: where alerts and digests go."""

    alerts: int | None = None
    digest: int | None = None


def _parse_topics(value: object) -> object:
    """`<chat>:alerts=<thread>,digest=<thread>;<chat>:…` → {chat: {alerts, digest}}."""
    if not isinstance(value, str):
        return value
    topics: dict[str, dict[str, str]] = {}
    for number, part in enumerate((p.strip() for p in value.split(";")), start=1):
        if not part:
            continue
        chat, sep, rest = part.partition(":")
        pairs = [pair.partition("=") for pair in rest.split(",") if pair.strip()]
        if not sep or not chat.strip() or not pairs or any(not eq for _k, eq, _v in pairs):
            raise ValueError(
                f"notifications.telegram.topics: entry {number} is not "
                "'<chat>:alerts=<thread>,digest=<thread>'"
            )
        topics[chat.strip()] = {k.strip(): v.strip() for k, _eq, v in pairs}
    return topics


class TelegramConfig(StrictModel):
    """The Telegram bot (docs/telegram.md, ADR-0013): alerts with feedback buttons,
    hourly digests, /status /live /quiet. Long polling only: no webhook, no open port."""

    enabled: bool = True
    token: SecretStr  # ${VIDETTE_TELEGRAM_BOT_TOKEN}; never a literal, never logged
    # Chats that may talk to the bot; everything else is ignored. "id1,id2" or a list.
    allowed_chats: list[int] = Field(min_length=1)
    topics: dict[int, TelegramTopic] = Field(default_factory=dict)
    # Who gets alerts and digests: "first" (only the first allowed chat: a trial run) or
    # "all". Commands are answered in every allowed chat either way.
    recipients: Literal["first", "all"] = "all"
    # Names of recognized people in messages and on the "This is ..." buttons. Off: "known".
    names: bool = False
    frames: int = Field(default=3, ge=1, le=9)  # album frames that show the subject
    max_video_mb: float = Field(default=20, gt=0, le=50)  # bigger clips are linked
    collapse: str = "5m"  # one camera's alerts within this window become one message
    # The day's digest, once, silently, at this local time ("HH:MM"), and only when it has
    # something that needs a look; null: only on request (/status, or asking the bot).
    digest_at: str | None = "21:00"
    # Removed (the hourly digest was noise): accepted and ignored, `vidette validate` says so.
    digest_every: str | None = None
    digest_settle: str | None = None
    quiet_for: str = "1h"  # /quiet
    labels: bool = True  # answers become reviewed labels in the clip's dataset
    camera_names: dict[str, str] = Field(default_factory=dict)  # camera → name in messages
    clip_url: str | None = None  # base URL for clips too big to send; default server.base_url
    api_url: str = "https://api.telegram.org"

    @model_validator(mode="before")
    @classmethod
    def _removed_keys(cls, data: object) -> object:
        if isinstance(data, dict) and "live" in data:
            raise ValueError(
                "notifications.telegram.live was removed: live view will be part of the "
                "Vidette app itself, not links to a separate restreamer; delete the "
                "section (docs/telegram.md)"
            )
        return data

    @field_validator("allowed_chats", mode="before")
    @classmethod
    def _chats_from_text(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]
        return value

    @field_validator("topics", mode="before")
    @classmethod
    def _topics_from_text(cls, value: object) -> object:
        return _parse_topics(value)

    @model_validator(mode="after")
    def _check(self) -> TelegramConfig:
        token = self.token.get_secret_value()
        if not re.match(r"^\d+:[A-Za-z0-9_-]{20,}$", token):
            raise ValueError(
                "notifications.telegram.token does not look like a bot token "
                "(<digits>:<letters>); reference it as ${VIDETTE_TELEGRAM_BOT_TOKEN}"
            )
        if len(set(self.allowed_chats)) != len(self.allowed_chats):
            raise ValueError("notifications.telegram.allowed_chats lists a chat twice")
        for chat in self.topics:
            if chat not in self.allowed_chats:
                raise ValueError(
                    "notifications.telegram.topics names a chat that is not in allowed_chats"
                )
        for key in ("collapse", "quiet_for"):
            _duration(getattr(self, key), f"notifications.telegram.{key}")
        if self.digest_at is not None and self.digest_minutes is None:
            raise ValueError(
                "notifications.telegram.digest_at must be a local time like 21:00, or null "
                "for a digest only on request"
            )
        _check_http_url(self.api_url, "notifications.telegram.api_url")
        self.api_url = self.api_url.rstrip("/")
        if self.clip_url is not None:
            _check_http_url(self.clip_url, "notifications.telegram.clip_url")
            self.clip_url = self.clip_url.rstrip("/")
        return self

    def seconds(self, key: Literal["collapse", "quiet_for"]) -> float:
        return _duration(getattr(self, key), key)

    @property
    def digest_minutes(self) -> int | None:
        """`digest_at` as minutes after local midnight; None when off or malformed."""
        match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", (self.digest_at or "").strip())
        return None if match is None else int(match.group(1)) * 60 + int(match.group(2))

    @property
    def recipient_chats(self) -> list[int]:
        return self.allowed_chats[:1] if self.recipients == "first" else list(self.allowed_chats)


class NotificationsConfig(StrictModel):
    channels: dict[str, ChannelConfig] = Field(default_factory=dict)
    rules: list[NotifyRule] = Field(default_factory=list)
    telegram: TelegramConfig | None = None


class MqttConfig(StrictModel):
    """Vidette's own outbound MQTT (verdict events, HA discovery). May be the same broker
    Frigate uses; it is configured separately on purpose."""

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


class CrossCameraConfig(StrictModel):
    """The other cameras at the moment of an event (ADR-0017): which clips count as "at
    the same moment", and which cameras' clips can arrive late."""

    before: str = "15s"  # look this long before the moment (when a person was first seen)
    after: str = "45s"  # ...and this long after it
    # Cameras whose video reaches the collector late (a battery camera behind a base
    # station: measured 0 to 100 s, different every wake). Their clips are searched that
    # much later and shown as approximate in time. camera id → duration.
    lag_up_to: dict[str, str] = Field(default_factory=dict)
    max_video_mb: float = Field(default=19, gt=0, le=50)  # a longer mosaic is split

    @model_validator(mode="after")
    def _check(self) -> CrossCameraConfig:
        _duration(self.before, "cross_camera.before")
        _duration(self.after, "cross_camera.after")
        for camera, lag in self.lag_up_to.items():
            _duration(lag, f"cross_camera.lag_up_to.{camera}")
        return self

    def seconds(self, key: Literal["before", "after"]) -> float:
        return _duration(getattr(self, key), key)

    def lag_s(self, camera: str) -> float:
        lag = self.lag_up_to.get(camera)
        return 0.0 if lag is None else _duration(lag, camera)


class AssistantConfig(StrictModel):
    """Questions to the bot in plain words, and the same tools over MCP (ADR-0017).

    The model is one of the judge's endpoints: `local` (understanding.vlm.local, the
    default: nothing leaves your network) or `cloud` (understanding.vlm.cloud: the question
    and the facts the tools return go to that vendor, never a name, a face or a frame)."""

    enabled: bool = True
    backend: Literal["local", "cloud", "none"] = "local"
    max_calls_per_day: int = Field(default=300, ge=1)
    max_steps: int = Field(default=3, ge=1, le=6)  # tool rounds before it must answer
    timeout_s: float = Field(default=300, gt=0)  # one model call; a shared server queues


class VidetteConfig(StrictModel):
    server: ServerConfig = ServerConfig()
    storage: StorageConfig = StorageConfig()
    frigate: FrigateConfig | None = None
    clip_folders: list[ClipFolderConfig] = Field(default_factory=list)
    understanding: UnderstandingConfig = UnderstandingConfig()
    policies: list[PolicyConfig] = Field(default_factory=list)
    people: PeopleConfig = Field(default_factory=PeopleConfig)
    notifications: NotificationsConfig = NotificationsConfig()
    cross_camera: CrossCameraConfig = Field(default_factory=CrossCameraConfig)
    assistant: AssistantConfig = Field(default_factory=AssistantConfig)
    integrations: IntegrationsConfig = IntegrationsConfig()
    telemetry: TelemetryConfig = TelemetryConfig()

    @model_validator(mode="before")
    @classmethod
    def _v01_sections(cls, data: object) -> object:
        """Name the v0.1 NVR section instead of a bare 'extra inputs' error."""
        if isinstance(data, dict) and "cameras" in data:
            raise ValueError(
                "cameras: this is a v0.1 NVR section; since v0.2 Vidette reads cameras from "
                "Frigate (docs/architecture/adr/0009-intent-layer-over-frigate.md); configure "
                "them in Frigate, delete this section and add a `frigate:` section instead"
            )
        return data

    @model_validator(mode="after")
    def _cross_references(self) -> VidetteConfig:
        watched = None if self.frigate is None else self.frigate.cameras
        if isinstance(watched, list):
            watched = [*watched, *(folder.camera for folder in self.clip_folders)]
        names: set[str] = set()
        for policy in self.policies:
            if policy.name in names:
                raise ValueError(f"policy name '{policy.name}' is used twice")
            names.add(policy.name)
            if policy.cameras == "all":
                continue
            _check_camera_names(policy.cameras, f"policy '{policy.name}'")
            if isinstance(watched, list):
                for camera in policy.cameras:
                    if camera not in watched:
                        raise ValueError(
                            f"policy '{policy.name}' references camera '{camera}', which is "
                            "not in frigate.cameras (or a clip folder); add it there or use "
                            "cameras: all"
                        )
        for rule in self.notifications.rules:
            for channel in rule.channels:
                if channel not in self.notifications.channels:
                    raise ValueError(
                        f"notification rule '{rule.when}' references unknown channel '{channel}'"
                    )
        return self

    def watched_cameras(self) -> list[str] | None:
        """Explicit Frigate camera list, or None for `all` / no Frigate configured."""
        if self.frigate is None or self.frigate.cameras == "all":
            return None
        return list(self.frigate.cameras)


# --- honesty: designed-but-not-implemented warnings -------------------------------------------


def designed_feature_warnings(config: VidetteConfig) -> list[str]:
    """One warning per configured feature that is not working yet, with where it lands.

    Keep in sync with ROADMAP.md — the point is that `vidette validate` never lets a config
    silently pretend. Remove entries as features ship.
    """
    warnings: list[str] = []
    if config.frigate is None and not config.clip_folders:
        warnings.append(
            "no incident source: configure frigate: (docs/frigate.md) or clip_folders: "
            "(docs/clip-folders.md). Vidette starts, but receives no incidents"
        )
    if config.server.auth.mode is AuthMode.none:
        warnings.append(
            "server.auth.mode=none disables authentication — acceptable only on a trusted "
            "LAN/kiosk; a permanent UI banner will remind you"
        )
    vlm = config.understanding.vlm
    if vlm.cloud is not None:
        warnings.append(
            f"understanding.vlm.cloud: when the cloud backend is selected, frames from your "
            f"cameras are sent to {vlm.cloud.vendor} ({vlm.cloud.resolved_base_url})"
        )
        if not vlm.cloud.prices.is_set:
            warnings.append(
                "understanding.vlm.cloud.prices: not set, so reports show the cloud judge's "
                "cost as unknown; copy your provider's prices (docs/vlm.md)"
            )
    if config.policies and vlm.backend is VlmBackend.none:
        warnings.append(
            "policies: understanding.vlm.backend is none, so incidents stay 'observed' and "
            "nothing is notified until a backend is selected here or on the System page; "
            "measure a policy first with `vidette eval` (docs/vlm.md)"
        )
    if not config.policies:
        warnings.append("policies: none configured, so no incident is ever judged or notified")
    if config.people.enabled and not config.people.dir.is_dir():
        warnings.append(
            f"people.dir {config.people.dir} does not exist yet; add someone with "
            "`vidette people add NAME PHOTO…` or on the People page (docs/people.md)"
        )
    telegram = config.notifications.telegram
    if telegram is not None and telegram.enabled:
        if telegram.recipients == "first" and len(telegram.allowed_chats) > 1:
            warnings.append(
                "notifications.telegram.recipients: first, so alerts and digests go to the "
                "first allowed chat only (a trial run); set all when the others agreed"
            )
        if config.assistant.enabled and config.assistant.backend == "cloud":
            cloud = config.understanding.vlm.cloud
            where = "the cloud backend" if cloud is None else f"{cloud.vendor}"
            warnings.append(
                f"assistant.backend: cloud sends questions asked in Telegram and the facts the "
                f"tools find (times, cameras, what the judge said, identity numbers; never a "
                f"name, a face or a frame) to {where}"
            )
        removed = [k for k in ("digest_every", "digest_settle") if getattr(telegram, k)]
        if removed:
            warnings.append(
                f"notifications.telegram.{', '.join(removed)}: no longer used (there is no "
                "hourly digest any more); the day's digest goes out at digest_at (21:00) "
                "when it has something to say, and on request. Delete the key"
            )
        if telegram.names:
            warnings.append(
                "notifications.telegram.names: true sends the names of recognized people to "
                "Telegram's servers with each alert (docs/telegram.md#privacy)"
            )
    unless_known = [
        p.name
        for p in config.policies
        if any(level.wake is Wake.unless_known for level in p.levels.values())
    ]
    if unless_known and not config.people.enabled:
        warnings.append(
            f"policies {unless_known}: wake: unless_known cannot tell known people while "
            "people.enabled is false, so those levels wake everyone (except in quiet hours)"
        )
    quiet = [p.name for p in config.policies if p.known_people == "quiet"]
    if quiet and not config.people.enabled:
        warnings.append(
            f"policies {quiet}: known_people: quiet has no effect while people.enabled is false"
        )
    if config.people.track_unknown and not config.people.enabled:
        warnings.append(
            "people.track_unknown has no effect while people.enabled is false (the face "
            "check finds the faces it tracks)"
        )
    elif config.people.track_unknown:
        warnings.append(
            "people.track_unknown keeps face embeddings and crops of people who never agreed "
            f"to it, for {config.people.identities.retention_days} days, in "
            f"{config.people.identities.dir}; check the rules where you live "
            "(docs/people.md#privacy-and-the-law)"
        )
    recurring = [p.name for p in config.policies if p.recurrence is not None]
    if recurring and not (config.people.enabled and config.people.track_unknown):
        warnings.append(
            f"policies {recurring}: recurrence has no effect while people.enabled or "
            "people.track_unknown is false"
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
