"""`KEY=VALUE` files for secrets kept outside the config (`vidette serve --env-file`).

The format is the plain one systemd and Docker use, not shell: one assignment per line,
`#` comments and blank lines ignored, optional matching quotes around the value removed,
nothing expanded or executed. A value may contain `;`, `$` or spaces as they are. Variables
already set in the environment win, so a launchd or compose setting overrides the file.

The file should be readable by its owner only; a looser mode is reported, never printed.
Values are never printed either.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import MutableMapping
from pathlib import Path

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class EnvFileError(Exception):
    """The file cannot be read or has a line that is not KEY=VALUE (the value is not shown)."""


def parse_env_text(text: str, *, source: str = "env file") -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _KEY_RE.match(key):
            raise EnvFileError(f"{source}:{number}: expected KEY=VALUE")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def load_env_file(
    path: Path, environ: MutableMapping[str, str] | None = None
) -> tuple[list[str], list[str]]:
    """Set the file's variables that are not set yet. Returns (names set, warnings)."""
    target = os.environ if environ is None else environ
    try:
        text = path.read_text(encoding="utf-8")
        mode = path.stat().st_mode
    except OSError as exc:
        raise EnvFileError(f"cannot read {path}: {exc.strerror or exc}") from None
    values = parse_env_text(text, source=str(path))
    warnings = []
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        warnings.append(f"{path} is readable by other users; chmod 600 {path}")
    applied = []
    for key, value in values.items():
        if key not in target:
            target[key] = value
            applied.append(key)
    return applied, warnings
