"""Where a clip's face report lives in a dataset folder.

- A clip collected by `vidette collect` has a JSON sidecar (`clips/<id>.json`); the report
  goes into it under `people`, every other key untouched.
- Any other clip gets `faces/<id>/people.json`.

Face crops always go to `faces/<id>/<n>.jpg` (folder 0700, files 0600), and the report
names them relative to the dataset root.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from vidette.paths import private_dir, write_private
from vidette.people.check import CropWriter, crop_writer
from vidette.people.report import PeopleReport

FACES_DIR = "faces"
REPORT_FILE = "people.json"


def dataset_root(clip: Path) -> Path:
    """`<root>/clips/x.mp4` → `<root>`; a clip anywhere else → its own folder."""
    return clip.parent.parent if clip.parent.name == "clips" else clip.parent


def _sidecar(clip: Path) -> Path | None:
    sidecar = clip.with_suffix(".json")
    if not sidecar.is_file():
        return None
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return sidecar if isinstance(data, dict) and "clip" in data else None


def report_path(clip: Path) -> Path:
    return _sidecar(clip) or dataset_root(clip) / FACES_DIR / clip.stem / REPORT_FILE


def load_report(clip: Path) -> PeopleReport | None:
    path = report_path(clip)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    raw = data if path.name == REPORT_FILE else data.get("people")
    if raw is None:
        return None
    try:
        return PeopleReport.model_validate(raw)
    except ValidationError:
        return None


def save_report(clip: Path, report: PeopleReport) -> Path:
    path = report_path(clip)
    payload = report.model_dump(mode="json")
    if path.name == REPORT_FILE:
        write_private(path, (json.dumps(payload, indent=2) + "\n").encode())
        return path
    data = json.loads(path.read_text(encoding="utf-8"))
    data["people"] = payload
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.chmod(path.stat().st_mode & 0o777)
    tmp.replace(path)
    return path


def crops_for(clip: Path) -> CropWriter:
    """A writer for this clip's crops; crops from an earlier check are removed first."""
    root = dataset_root(clip)
    private_dir(root / FACES_DIR)
    folder = root / FACES_DIR / clip.stem
    if folder.is_dir():
        for old in folder.glob("*.jpg"):
            old.unlink()
    return crop_writer(root, f"{FACES_DIR}/{clip.stem}")
