"""The two model files face recognition needs, fetched once and checked byte for byte.

Both come from the OpenCV model zoo at a pinned commit, so the URL can never start
serving something else under the same name; the sha256 is checked after every download
and before every load. Files live in a cache outside any repository (default
`~/.cache/vidette/models`, or `$XDG_CACHE_HOME/vidette/models`, or `people.models_dir`).

| File | What | License |
|---|---|---|
| face_detection_yunet_2023mar.onnx | YuNet face detector, 5 landmarks | MIT |
| face_recognition_sface_2021dec.onnx | SFace face embedder (MobileFaceNet) | Apache-2.0 |

Nothing is downloaded silently: the command-line tools download on first use and say
what, from where and how big; `vidette serve` never downloads (`vidette people models`
fetches them for it). Licence choice and alternatives: ADR-0012.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ZOO_COMMIT = "48b10fa505aeb51fbd127fc60a5c660040e823b7"
_ZOO = f"https://github.com/opencv/opencv_zoo/raw/{ZOO_COMMIT}/models"


class ModelError(Exception):
    """A model file is missing, damaged or could not be downloaded; the message says
    what to do."""


@dataclass(frozen=True)
class ModelFile:
    name: str
    url: str
    sha256: str
    size: int
    license: str
    what: str


DETECTOR = ModelFile(
    name="face_detection_yunet_2023mar.onnx",
    url=f"{_ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    sha256="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    size=232_589,
    license="MIT",
    what="YuNet face detector",
)
RECOGNIZER = ModelFile(
    name="face_recognition_sface_2021dec.onnx",
    url=f"{_ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
    sha256="0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    size=38_696_353,
    license="Apache-2.0",
    what="SFace face embedder",
)
MODELS = (DETECTOR, RECOGNIZER)


def default_models_dir() -> Path:
    cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(cache) if cache else Path("~/.cache").expanduser()
    return base / "vidette" / "models"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verified(path: Path, spec: ModelFile) -> bool:
    try:
        return path.stat().st_size == spec.size and sha256_of(path) == spec.sha256
    except OSError:
        return False


Fetch = Callable[[str, Path], None]
Say = Callable[[str], None]


def _fetch_httpx(url: str, target: Path) -> None:
    import httpx

    with (
        httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as response,
        target.open("wb") as handle,
    ):
        response.raise_for_status()
        for chunk in response.iter_bytes():
            handle.write(chunk)


def ensure_model(
    spec: ModelFile,
    models_dir: Path,
    *,
    download: bool,
    say: Say = print,
    fetch: Fetch = _fetch_httpx,
) -> Path:
    """The verified file, downloading it first when allowed. A file that fails the
    check is never loaded; with `download` it is replaced."""
    path = models_dir / spec.name
    if path.is_file():
        if verified(path, spec):
            return path
        if not download:
            raise ModelError(
                f"{path} does not match its checksum (damaged or replaced); delete it and "
                "run `vidette people models`"
            )
        say(f"  {spec.name} does not match its checksum; downloading it again")
    elif not download:
        raise ModelError(
            f"face model {spec.name} is not downloaded; run `vidette people models` "
            f"(it fetches {spec.size / 1e6:.1f} MB from github.com/opencv/opencv_zoo into "
            f"{models_dir})"
        )
    models_dir.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    say(f"  downloading {spec.what} ({spec.license}, {spec.size / 1e6:.1f} MB) from {spec.url}")
    try:
        fetch(spec.url, part)
    except Exception as exc:
        part.unlink(missing_ok=True)
        raise ModelError(f"cannot download {spec.name}: {exc}") from exc
    if not verified(part, spec):
        got = sha256_of(part) if part.is_file() else "nothing"
        part.unlink(missing_ok=True)
        raise ModelError(
            f"downloaded {spec.name} does not match its pinned sha256 ({got[:16]}…); "
            "nothing was installed"
        )
    part.replace(path)
    return path


def ensure_models(
    models_dir: Path, *, download: bool, say: Say = print, fetch: Fetch = _fetch_httpx
) -> tuple[Path, Path]:
    """(detector, recognizer) paths, verified."""
    return (
        ensure_model(DETECTOR, models_dir, download=download, say=say, fetch=fetch),
        ensure_model(RECOGNIZER, models_dir, download=download, say=say, fetch=fetch),
    )
