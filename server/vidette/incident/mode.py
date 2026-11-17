"""Incident mode (ADR-0022): after an alert, every camera of the site keeps what it sees.

    vidette serve: an alert ─▶ incident-mode.json {since, until, event, camera, level}
    vidette collect (every camera): reads it ─▶ until `until`, every woken stream is kept
        whole as clips (no motion gate, no minimum length), and a continuous camera keeps
        the stretches its motion gate did not cut
    vidette serve: the pre-filter never skips a clip from those minutes, and the judge is
        told what the alert was

A plain file next to serve's database, written atomically: the collectors are separate
processes (one per camera) that already read serve's config for `--prune-oldest`; a file
needs no port, no auth and survives either side restarting. A missing, unreadable or
expired file means no incident: incident mode can only ever keep more, never less.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

INCIDENT_FILE = "incident-mode.json"
CHECK_EVERY_S = 1.0  # the collectors look at the file at most this often


@dataclass(frozen=True)
class IncidentMode:
    since: float  # epoch seconds: the alert's moment (its clip's start)
    until: float  # every camera keeps everything until then
    event_id: str  # the alert that started it
    camera: str
    level: str | None = None
    label: str | None = None  # the alert's scenario

    def active(self, now: float) -> bool:
        return now < self.until

    def extended(self, other: IncidentMode) -> IncidentMode:
        """One incident mode for two alerts: the earliest start, the latest end; the
        newer alert names it."""
        return IncidentMode(
            since=min(self.since, other.since),
            until=max(self.until, other.until),
            event_id=other.event_id,
            camera=other.camera,
            level=other.level,
            label=other.label,
        )


def incident_path(database: Path) -> Path:
    """Where serve writes it and the collectors look: next to serve's database."""
    return database.expanduser().parent / INCIDENT_FILE


def read_incident(path: Path) -> IncidentMode | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return IncidentMode(
            since=float(data["since"]),
            until=float(data["until"]),
            event_id=str(data["event_id"]),
            camera=str(data["camera"]),
            level=None if data.get("level") is None else str(data["level"]),
            label=None if data.get("label") is None else str(data["label"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_incident(path: Path, mode: IncidentMode, *, now: float | None = None) -> IncidentMode:
    """Write `mode`, merged with one still active in the file (two alerts a minute apart
    are one incident). Returns what was written."""
    current = read_incident(path)
    clock = time.time() if now is None else now
    if current is not None and current.active(clock):
        mode = current.extended(mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(asdict(mode)) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)
    tmp.replace(path)
    return mode


class IncidentWatch:
    """The collector's view of the file: re-read when it changed, at most every second."""

    def __init__(self, path: Path | None, *, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self._clock = clock
        self._checked = float("-inf")
        self._stamp: tuple[int, int] | None = None
        self._mode: IncidentMode | None = None

    def active(self) -> IncidentMode | None:
        """The incident mode in force now, or None."""
        if self.path is None:
            return None
        now = time.monotonic()
        if now - self._checked >= CHECK_EVERY_S:
            self._checked = now
            try:
                stat = self.path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                stamp = None
            if stamp != self._stamp:
                self._stamp = stamp
                self._mode = None if stamp is None else read_incident(self.path)
        mode = self._mode
        return mode if mode is not None and mode.active(self._clock()) else None
