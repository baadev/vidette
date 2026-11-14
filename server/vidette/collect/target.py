"""The camera's RTSP address: where it comes from and how it never leaks.

Eufy's NAS (RTSP) URL carries the camera's username and password
(`rtsp://user:password@10.0.20.30/live0`). Rules (CLAUDE.md, security model):

- it is read from the environment (`VIDETTE_RTSP_URL`) or from a file outside the
  repository (default `~/.config/vidette/rtsp-url`, one line), never from a flag, a config
  committed to git or the dataset folder;
- everything Vidette prints, logs or writes goes through `RtspTarget.scrub()`, which
  removes the URL, its user info and the password in raw and percent-encoded form,
  including inside ffmpeg's own error lines (ffmpeg echoes the full input URL);
- `str()` and `repr()` of a target show only `rtsp://***@host:port/path`.

What cannot be hidden: ffmpeg receives the URL as a command-line argument, so another
user account on the same machine could read it with `ps` while a recording runs.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

ENV_VAR = "VIDETTE_RTSP_URL"
DEFAULT_URL_FILE = Path("~/.config/vidette/rtsp-url")
_REDACTED = "***"


class TargetError(Exception):
    """The RTSP address is missing or unusable; the message says what to do (no secrets)."""


@dataclass(frozen=True)
class RtspTarget:
    url: str = field(repr=False)
    host: str
    port: int
    path: str
    origin: str  # where the URL came from, for messages: "VIDETTE_RTSP_URL" or a file path

    @classmethod
    def parse(cls, url: str, origin: str) -> RtspTarget:
        url = url.strip()
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError as exc:
            raise TargetError(f"the RTSP URL from {origin} is not a valid URL") from exc
        if parts.scheme not in ("rtsp", "rtsps"):
            raise TargetError(
                f"the URL from {origin} must start with rtsp:// (Eufy app → camera → "
                "Settings → Storage → NAS (RTSP) shows it)"
            )
        if not parts.hostname:
            raise TargetError(f"the RTSP URL from {origin} has no host")
        return cls(
            url=url,
            host=parts.hostname,
            port=port or (322 if parts.scheme == "rtsps" else 554),
            path=parts.path or "/",
            origin=origin,
        )

    @property
    def redacted(self) -> str:
        parts = urlsplit(self.url)
        auth = f"{_REDACTED}@" if parts.username or parts.password else ""
        return f"{parts.scheme}://{auth}{self.host}:{self.port}{self.path}"

    def secrets(self) -> list[str]:
        """Every spelling of a secret that could show up in text, longest first."""
        parts = urlsplit(self.url)
        found = {self.url}
        if parts.netloc and "@" in parts.netloc:
            userinfo = parts.netloc.rsplit("@", 1)[0]
            found.add(userinfo)
        if parts.password:
            for value in (parts.password, unquote(parts.password), quote(parts.password)):
                if len(value) >= 3:  # never scrub every "a" out of a log line
                    found.add(value)
        return sorted(found, key=len, reverse=True)

    def scrub(self, text: str) -> str:
        for secret in self.secrets():
            text = text.replace(secret, self.redacted if secret == self.url else _REDACTED)
        return text

    def __str__(self) -> str:
        return self.redacted


def load_target(
    *, url_file: Path | None = None, env: Mapping[str, str] | None = None
) -> tuple[RtspTarget, list[str]]:
    """The camera URL from `VIDETTE_RTSP_URL`, else `url_file`, else the default file.

    Returns the target and warnings (for example a file other users can read).
    """
    environ = os.environ if env is None else env
    if environ.get(ENV_VAR):
        return RtspTarget.parse(environ[ENV_VAR], ENV_VAR), []
    path = (url_file or DEFAULT_URL_FILE).expanduser()
    if not path.is_file():
        raise TargetError(
            f"no camera address: set {ENV_VAR} or put the RTSP URL in {path} "
            f"(one line; then `chmod 600 {path}`)"
        )
    warnings: list[str] = []
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        warnings.append(
            f"{path} can be read by other users of this machine; run `chmod 600 {path}`"
        )
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(lines) != 1:
        raise TargetError(f"{path} must contain exactly one line: the camera's RTSP URL")
    return RtspTarget.parse(lines[0], str(path)), warnings
