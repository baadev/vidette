"""Dataset entries for clips: what `vidette label draft` and live feedback both append."""

from __future__ import annotations

import json
from pathlib import Path

from vidette.collect.clips import load_meta
from vidette.collect.collector import STATE_DIR, STATE_FILE
from vidette.eval.dataset import UNLABELLED, VIDEO_SUFFIXES, Dataset, Entry


def watched_hours(root: Path) -> float | None:
    try:
        data = json.loads((root / STATE_DIR / STATE_FILE).read_text(encoding="utf-8"))
        return float(data["watched_seconds"]) / 3600
    except (OSError, ValueError, KeyError, TypeError):
        return None


def new_clips(dataset: Dataset) -> list[Path]:
    referenced = {item for entry in dataset.labels.entries for item in entry.media}
    clips = sorted(
        path
        for path in (dataset.root / "clips").glob("*")
        if path.suffix.lower() in VIDEO_SUFFIXES
        and path.relative_to(dataset.root).as_posix() not in referenced
    )
    return clips


def clip_entry(root: Path, clip: Path, number: int) -> Entry:
    meta = load_meta(clip.with_suffix(".json"))
    media = [clip.relative_to(root).as_posix()]
    if meta is not None:
        media += [frame for frame in meta.frames if (root / frame).is_file()]
        return Entry(
            id=meta.id,
            scenario=UNLABELLED,
            number=number,
            camera=meta.camera,
            at=meta.started_at,
            duration_s=meta.duration_s,
            media=media,
        )
    frames_dir = root / "frames" / clip.stem
    if frames_dir.is_dir():
        media += sorted(
            (p.relative_to(root).as_posix() for p in frames_dir.glob("*.jpg")),
            key=lambda name: (len(name), name),
        )
    entry_id = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in clip.stem)
    return Entry(id=entry_id.strip("-.") or f"clip-{number}", scenario=UNLABELLED,
                 number=number, media=media)  # fmt: skip
