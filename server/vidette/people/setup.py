"""Building the people store and the face check from a config (CLI tools and server)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from vidette.core.config import PeopleConfig
from vidette.people.check import CheckSettings, PeopleCheck
from vidette.people.engine import FaceLimits, OpenCvFaceEngine, load_engine
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


def check_for(
    config: PeopleConfig,
    *,
    download: bool,
    root: Path | None = None,
    say: Callable[[str], None] = print,
) -> PeopleCheck:
    return PeopleCheck(
        store_for(config, root=root),
        engine_for(config, download=download, say=say),
        CheckSettings(fps=config.frames_per_second, max_frames=config.max_frames),
    )
