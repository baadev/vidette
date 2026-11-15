"""Clip folders as an incident source: finished clips in, `incident.*` on the bus out.

For cameras that cannot feed Frigate (ADR-0010): a battery camera collected with
`vidette collect`, or any camera that uploads motion clips to a folder (FTP, SMB, a
synced phone export). Each clip is one incident, published as `incident.opened` then
`incident.closed`; the intake, the events API and the judges do not know the difference
from a Frigate review item.

What a clip says about itself:

- a `vidette collect` sidecar (`clips/<id>.json`) gives the camera, start, end and key
  frames;
- otherwise a `YYYYMMDD-HHMMSS` (or `YYYY-MM-DD_HH-MM-SS`) stamp in the file name gives
  the start, the file's modification time the end, and the folder's `camera` the camera.

Rules: a file must be unchanged for `settle` before it is read (uploads in progress);
hidden folders (`.partial`, `.vidette`) are skipped, and in a `vidette collect` dataset
(it has a `.vidette` folder) only `clips/` is read, so the continuous mode's lookback ring
(`ring/*.mkv`) never becomes an incident. An incident's `source_id` is the clip's path
as configured (folder + relative path), so two folders that both hold
`clips/20261114-210304.mp4` give two events, not one. On the very first start only clips
newer than that moment count, unless `backfill: true`, so pointing Vidette at a folder of
600 old clips does not produce 600 events. Progress (an arrival-time watermark per folder)
is kept in the database, so clips that arrive while Vidette is down are picked up
on the next start. Vidette never writes to the folder.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from vidette.collect.clips import CLIPS_DIR, EARLY_DIR, ClipMeta, load_early, load_meta
from vidette.core.config import ClipFolderConfig
from vidette.core.events import InProcessEventBus
from vidette.core.incidents import Incident, Phase, as_payload, incident_topic

logger = logging.getLogger(__name__)

SOURCE = "clip-folder"
VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv"})
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
_STAMP_RES = (
    re.compile(r"(?P<d>\d{8})[-_T](?P<t>\d{6})"),
    re.compile(r"(?P<d>\d{4}-\d{2}-\d{2})[ _T](?P<t>\d{2}[-:.]\d{2}[-:.]\d{2})"),
)
_ERROR_EMIT_EVERY = 20

EmitCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


class FolderStore(Protocol):
    """The slice of `Database` the source needs: its watermark and known incidents."""

    async def get_meta(self, key: str) -> str | None: ...

    async def set_meta(self, key: str, value: str) -> None: ...


@dataclass
class FolderStatus:
    path: str
    camera: str
    exists: bool
    clips_total: int = 0
    errors_total: int = 0
    last_clip_at: float | None = None
    last_scan_at: float | None = None


def stamp_from_name(name: str) -> float | None:
    """Local wall-clock time encoded in a clip's file name, as an epoch, or None."""
    for pattern in _STAMP_RES:
        match = pattern.search(name)
        if match is None:
            continue
        date = match.group("d").replace("-", "")
        clock = re.sub(r"[-:.]", "", match.group("t"))
        try:
            return datetime.strptime(date + clock, "%Y%m%d%H%M%S").timestamp()
        except ValueError:
            continue
    return None


def clip_incident(folder: ClipFolderConfig, clip: Path) -> Incident:
    """The incident one clip stands for (pure apart from reading its sidecar and frames)."""
    root = folder.path
    meta: ClipMeta | None = load_meta(clip.with_suffix(".json"))
    mtime = clip.stat().st_mtime
    frames: list[Path] = []
    if meta is not None:
        camera = meta.camera
        started = meta.started_at.timestamp()
        ended = meta.ended_at.timestamp()
        frames = [root / frame for frame in meta.frames]
    else:
        camera = folder.camera
        stamped = stamp_from_name(clip.stem)
        started = stamped if stamped is not None and stamped <= mtime else mtime
        ended = mtime
        sibling_frames = root / "frames" / clip.stem
        if sibling_frames.is_dir():
            frames = sorted(
                (p for p in sibling_frames.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES),
                key=lambda p: (len(p.stem), p.stem),
            )
    return Incident(
        source=SOURCE,
        # Unique across folders: two cameras often cut clips in the same second.
        source_id=(root / clip.relative_to(root)).as_posix(),
        camera=camera,
        started_at=started,
        ended_at=max(started, ended),
        media=[str(clip), *(str(frame) for frame in frames if frame.is_file())],
        timeline=clip_timeline(meta, clip),
    )


def clip_timeline(meta: ClipMeta | None, clip: Path) -> dict[str, float]:
    """The sidecar's stages; for a sidecar without them (an older collector), the first
    frame from its start and the clip closing from when the sidecar was written."""
    if meta is None:
        return {}
    timeline = dict(meta.timeline)
    timeline.setdefault("first_frame", meta.started_at.timestamp())
    with contextlib.suppress(OSError):
        timeline.setdefault("closed", clip.with_suffix(".json").stat().st_mtime)
    return timeline


def early_incident(folder: ClipFolderConfig, sidecar: Path) -> Incident | None:
    """The incident an early window stands for: the same id as the clip that will follow
    (`clips/<id>.mp4`), `partial`, its media the early MP4. None when unreadable."""
    root = folder.path
    meta = load_early(sidecar)
    if meta is None:
        return None
    video = root / meta.clip
    if not video.is_file():
        return None
    started = meta.started_at.timestamp()
    return Incident(
        source=SOURCE,
        source_id=(root / CLIPS_DIR / f"{meta.id}.mp4").as_posix(),
        camera=meta.camera,
        started_at=started,
        ended_at=started + meta.window_s,
        media=[str(video)],
        partial=True,
        timeline=dict(meta.timeline),
    )


def _watermark_key(folder: ClipFolderConfig) -> str:
    return f"clip_folder.watermark:{folder.path.resolve()}"


EARLY_POLL_S = 1.0  # early windows are looked for this often: they exist to save seconds
COLLECT_POLL_S = 2.0  # a `vidette collect` dataset's clips/ is listed at least this often
EARLY_MAX_AGE_S = 120.0  # an early window older than this is not worth an early verdict


COLLECT_STATE_DIR = ".vidette"  # `vidette collect` keeps its state here (collector.STATE_DIR)


def find_clips(root: Path) -> list[Path]:
    """Video files under `root`, skipping hidden folders and files. In a `vidette
    collect` dataset only `clips/` counts: its `ring/` holds lookback segments, not clips."""
    found: list[Path] = []
    scan = root / "clips" if (root / COLLECT_STATE_DIR).is_dir() else root
    if not scan.is_dir():
        return found
    for path in scan.rglob("*"):
        relative = path.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES:
            found.append(path)
    return found


class ClipFolderSource:
    def __init__(
        self,
        folders: list[ClipFolderConfig],
        bus: InProcessEventBus,
        store: FolderStore,
        *,
        emit: EmitCallback,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._folders = folders
        self._bus = bus
        self._store = store
        self._emit = emit
        self._clock = clock
        self._status = {
            str(folder.path): FolderStatus(
                path=str(folder.path), camera=folder.camera, exists=folder.path.is_dir()
            )
            for folder in folders
        }
        self._tasks: list[asyncio.Task[None]] = []
        self._early_seen: set[str] = set()
        self._started = clock()  # early windows from before this start are not judged

    def status(self) -> list[FolderStatus]:
        return list(self._status.values())

    def start(self) -> None:
        if self._tasks:
            return
        for folder in self._folders:
            self._tasks.append(
                asyncio.create_task(self._poll(folder), name=f"clip-folder:{folder.path}")
            )

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _poll(self, folder: ClipFolderConfig) -> None:
        next_scan = 0.0
        while True:
            try:
                if (folder.path / EARLY_DIR).is_dir():
                    await self.scan_early(folder)
                if time.monotonic() >= next_scan:
                    every = folder.poll_every_s
                    if (folder.path / COLLECT_STATE_DIR).is_dir():
                        every = min(every, COLLECT_POLL_S)  # its sidecar says "done"
                    next_scan = time.monotonic() + every
                    await self.scan(folder)
            except Exception as exc:  # crash containment: a bad scan never stops the loop
                logger.exception("clip folder %s: scan failed", folder.path)
                await self._report_error(folder, f"scan failed: {exc}")
            await asyncio.sleep(min(EARLY_POLL_S, folder.poll_every_s))

    async def scan_early(self, folder: ClipFolderConfig) -> list[Incident]:
        """Publish each new early window (`.early/<id>.json`, written last and atomically
        by `vidette collect --early`) as a partial incident, unless its clip is already
        there or it is too old. Public for tests."""
        now = self._clock()
        published: list[Incident] = []
        early = folder.path / EARLY_DIR
        try:
            sidecars = sorted(early.glob("*.json"))
        except OSError:
            return published
        for sidecar in sidecars:
            key = str(sidecar)
            if key in self._early_seen:
                continue
            try:
                arrived = sidecar.stat().st_mtime
            except OSError:
                continue
            self._early_seen.add(key)
            if now - arrived > EARLY_MAX_AGE_S or arrived < self._started:
                continue
            if (folder.path / CLIPS_DIR / f"{sidecar.stem}.mp4").exists():
                continue  # the clip is already here: it will be judged whole
            incident = early_incident(folder, sidecar)
            if incident is None:
                continue
            incident = incident.model_copy(
                update={"timeline": {**incident.timeline, "picked_up": now}}
            )
            await self._bus.publish(incident_topic(Phase.updated), as_payload(incident))
            published.append(incident)
        if len(self._early_seen) > 2000:
            self._early_seen = {k for k in self._early_seen if Path(k).exists()}
        return published

    async def _report_error(self, folder: ClipFolderConfig, error: str) -> None:
        status = self._status[str(folder.path)]
        status.errors_total += 1
        if status.errors_total == 1 or status.errors_total % _ERROR_EMIT_EVERY == 0:
            await self._emit(
                "clip_folder.error",
                {"path": str(folder.path), "error": error[:300], "count": status.errors_total},
            )

    async def scan(self, folder: ClipFolderConfig) -> list[Incident]:
        """Publish every settled clip newer than the folder's watermark. Public for tests."""
        status = self._status[str(folder.path)]
        now = self._clock()
        status.last_scan_at = now
        status.exists = folder.path.is_dir()
        if not status.exists:
            return []
        key = _watermark_key(folder)
        raw = await self._store.get_meta(key)
        if raw is None:
            watermark = 0.0 if folder.backfill else now
            await self._store.set_meta(key, repr(watermark))
        else:
            watermark = float(raw)

        candidates = await asyncio.to_thread(find_clips, folder.path)
        collected = (folder.path / COLLECT_STATE_DIR).is_dir()
        fresh: list[tuple[float, Path]] = []
        for clip in candidates:
            try:
                st = clip.stat()
            except OSError:
                continue
            # Arrival, not recording time: a synced file can keep an old mtime, but its
            # inode change time is when it landed here.
            arrived = max(st.st_mtime, st.st_ctime)
            # `vidette collect` writes the sidecar last, atomically: with it, the clip is
            # complete, and waiting for `settle` would only delay the alert.
            done = collected and clip.with_suffix(".json").is_file()
            if arrived > watermark and (done or now - arrived >= folder.settle_s):
                fresh.append((arrived, clip))

        published: list[Incident] = []
        for arrived, clip in sorted(fresh):
            try:
                incident = clip_incident(folder, clip)
            except (OSError, ValueError) as exc:
                await self._report_error(folder, f"{clip.name}: {exc}")
                continue
            incident = incident.model_copy(
                update={"timeline": {**incident.timeline, "picked_up": self._clock()}}
            )
            for phase in (Phase.opened, Phase.closed):
                await self._bus.publish(incident_topic(phase), as_payload(incident))
            published.append(incident)
            status.clips_total += 1
            status.last_clip_at = incident.started_at
            await self._store.set_meta(key, repr(arrived))
        return published
