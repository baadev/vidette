"""The repository's privacy check (.github/scripts/privacy_check.py): it must pass on the
repository as it is, and refuse the things a public repo must never carry."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / ".github" / "scripts" / "privacy_check.py"


@pytest.fixture(scope="module")
def check() -> ModuleType:
    spec = importlib.util.spec_from_file_location("privacy_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_repository_passes() -> None:
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout")
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=REPO, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    "line",
    [
        # Each sample carries the allow mark so this file passes the check itself; the
        # mark is in the comment, not in the string that is checked.
        "url: rtsp://admin:hunter2@192.168.1.8:554/live0",  # privacy-check: allow
        "rtsp://cam:Winter2026!@camera.home.arpa/stream",  # privacy-check: allow
        "base_url: http://192.168.7.21:11434/v1",  # privacy-check: allow
        "the box at 172.20.1.4 and 10.1.2.3",  # privacy-check: allow
        "contact me at someone.private@gmail.com",  # privacy-check: allow
    ],
)
def test_it_refuses(check: ModuleType, line: str) -> None:
    assert check.check_text("docs/x.md", line)


@pytest.mark.parametrize(
    "line",
    [
        "main: rtsp://user:${EUFY_RTSP_PASSWORD}@10.0.20.30/live0",
        'f"rtsp://admin:{PASSWORD}@10.0.20.30/live0"',
        "rtsp://u:p@IP:554/Streaming/Channels/101",
        "rtsp://USER:PASSWORD@192.0.2.50/live0",
        "rtsp://***@10.0.20.30:554/live0",
        "rtsp://u:secret@127.0.0.1:9/live0",
        "http://localhost:11434/v1 and 0.0.0.0:8642, version 1.2.3",
        "alex@baadev.com, noreply@anthropic.com, someone@example.org",
        "base_url: http://192.168.1.20/v1  # privacy-check: allow",
    ],
)
def test_it_lets_examples_through(check: ModuleType, line: str) -> None:
    assert check.check_text("docs/x.md", line) == []


@pytest.mark.parametrize(
    ("path", "refused"),
    [
        ("datasets/synthetic-sample/frames/cat-night/1.jpg", False),
        ("docs/img/sheet.png", False),
        ("web/public/favicon.svg", False),
        ("server/tests/fixtures/frigate/api/snapshot.jpg", False),
        ("web/src/api.ts", False),
        ("server/vidette/people/store.py", False),
        ("server/tests/fixtures/me.jpg", False),
        ("selfie.jpg", True),
        ("server/vidette/people/grid.png", True),
        ("docs/faces/sam.jpg", True),
        ("docs/identities/crops/12.jpg", True),
        ("server/vidette/people/identities.py", False),
        ("datasets/yard/clips/20261114-210304.mp4", True),
        ("models/face_recognition_sface_2021dec.onnx", True),
        ("web/public/embeddings.npy", True),
    ],
)
def test_media_only_where_allowed(check: ModuleType, path: str, refused: bool) -> None:
    assert bool(check.check_media(path)) is refused
