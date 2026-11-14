"""Filesystem rules shared by the tools that keep private data (footage, faces)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def git_work_tree(path: Path) -> Path | None:
    """The git work tree containing `path`, if any (a `.git` dir or file up the tree)."""
    for parent in [path, *path.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def private_dir(path: Path) -> Path:
    """Create `path` (and parents) and make it readable by its owner only (0700)."""
    path.mkdir(parents=True, exist_ok=True)
    if stat.S_IMODE(path.stat().st_mode) != PRIVATE_DIR_MODE:
        path.chmod(PRIVATE_DIR_MODE)
    return path


def write_private(path: Path, data: bytes) -> None:
    """Write atomically with mode 0600, whatever the umask (temp file, then rename)."""
    private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        tmp.chmod(PRIVATE_FILE_MODE)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def too_open(path: Path) -> bool:
    """True when group or others can read or enter `path`."""
    return bool(stat.S_IMODE(path.stat().st_mode) & 0o077)
