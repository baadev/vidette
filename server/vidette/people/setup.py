"""Building the people store and the face check from a config (CLI tools and server)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from vidette.core.config import PeopleConfig
from vidette.people.check import CheckSettings, PeopleCheck
from vidette.people.engine import FaceLimits, OpenCvFaceEngine, load_engine
from vidette.people.identities import IdentitySettings, IdentityStore
from vidette.people.models import default_models_dir
from vidette.people.store import PeopleStore


def store_for(config: PeopleConfig, *, root: Path | None = None) -> PeopleStore:
    """The people folder from the config (or `root`); StoreError inside a git tree."""
    return PeopleStore(
        root or config.dir,
        threshold=config.threshold,
        max_faces=config.max_faces_per_person,
    )


def engine_for(
    config: PeopleConfig, *, download: bool, say: Callable[[str], None] = print
) -> OpenCvFaceEngine:
    """EngineUnavailableError without OpenCV; ModelError when the models are missing
    (and `download` is off) or fail their checksum."""
    return load_engine(
        config.models_dir or default_models_dir(),
        download=download,
        limits=FaceLimits(min_face_px=config.min_face_px),
        say=say,
    )


def identities_for(config: PeopleConfig) -> IdentityStore:
    """The anonymous identities (ADR-0014); IdentityError inside a git tree. Only
    `vidette serve` and the backfill track identities: `match`, `scan`, `label` and `eval`
    never add to them."""
    settings = config.identities
    return IdentityStore(
        settings.dir,
        IdentitySettings(
            match=settings.match,
            margin=settings.margin,
            min_face_px=settings.min_face_px,
            near_known=settings.near_known,
            retention_days=settings.retention_days,
        ),
    )


def check_for(
    config: PeopleConfig,
    *,
    download: bool,
    root: Path | None = None,
    say: Callable[[str], None] = print,
    identities: IdentityStore | None = None,
) -> PeopleCheck:
    return PeopleCheck(
        store_for(config, root=root),
        engine_for(config, download=download, say=say),
        CheckSettings(
            fps=config.frames_per_second,
            max_frames=config.max_frames,
            extra_fps=config.extra_fps,
            extra_frames=config.extra_frames,
        ),
        identities,
    )
