"""The other cameras at the moment of an event (ADR-0017, docs/other-cameras.md).

An event is one clip of one camera. A person walking along a street is usually on two or
three cameras within a minute, and a battery camera records only while it is awake, so
each camera has its own clips with gaps between them. For an event this module finds
every clip of every clip-folder camera that overlaps a window around the event's moment
(by default 15 s before to 45 s after the moment a person was first seen), the event's
own camera included, and says what is known about each:

- the clip's own event, when Vidette made one of it: the verdict's scenario and how many
  people the judge counted;
- **the same person**, only by face: an anonymous identity or an enrolled person found in
  both this event and that clip (ADR-0014, ADR-0016). Without a face nothing says "the
  same person"; a clip at the same moment is only that. Nothing about how anyone looks
  (clothes, height, colour) is ever used to link people;
- whether its place in time is **approximate**: clip times are the collector's stamps on
  each camera's clock (ADR-0015), within a few seconds of the cameras' on-screen clocks for
  cameras that stream all the time, but a camera whose video reaches the collector late
  (`cross_camera.lag_up_to`) may show a moment up to that long before its stamp. Such a
  camera's clips are searched that much later and marked approximate.

Found on demand from the clip folders, never stored: a clip that arrives after the event
is found too. Read-only: the folders are not touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from vidette.clipfolder import SOURCE, clip_incident, find_clips, stamp_from_name
from vidette.collect.clips import load_meta
from vidette.core.config import ClipFolderConfig, VidetteConfig
from vidette.db import EventRow
from vidette.stitch import Piece, Stitched, Tile, stitch
from vidette.telegram.i18n import t

STAMP_SLACK_S = 600.0  # a clip's name is its start; clips can be long (a wake period)
_STAMP = re.compile(r"(\d{8})-(\d{6})")


@dataclass(frozen=True)
class Window:
    moment: float  # when a person was first seen, else the event's start
    start: float
    end: float


@dataclass
class LinkedClip:
    camera: str
    path: Path
    started_at: float
    ended_at: float
    own: bool = False  # the event's own clip
    approximate: bool = False  # this camera's clips can arrive late: its time is a guess
    event_id: str | None = None  # the clip's own event, when there is one
    scenario: str | None = None  # that event's verdict
    persons: int | None = None
    identities: set[int] = field(default_factory=set)  # faces in it (identity numbers)
    people: set[str] = field(default_factory=set)  # enrolled people recognized in it
    same_person: bool = False  # a face also seen in the event (identity or enrolled)

    def overlap(self, window: Window) -> float:
        return max(0.0, min(self.ended_at, window.end) - max(self.started_at, window.start))


@dataclass
class Linked:
    event_id: str
    camera: str
    window: Window
    clips: list[LinkedClip]  # the event's own first, then by start time

    @property
    def others(self) -> list[LinkedClip]:
        return [clip for clip in self.clips if not clip.own]

    def cameras(self) -> list[str]:
        seen: list[str] = []
        for clip in self.clips:
            if clip.camera not in seen:
                seen.append(clip.camera)
        return seen

    def silent_cameras(self, configured: Iterable[str]) -> list[str]:
        """Configured cameras with no clip in the window: asleep, or nothing moved."""
        have = set(self.cameras())
        return [camera for camera in configured if camera not in have]


def moment_of(row: EventRow) -> float:
    """When the event's subject was first seen (the judge's first frame with a person),
    else when its clip started."""
    timeline = (row.intent or {}).get("timeline")
    if isinstance(timeline, Mapping):
        seen = timeline.get("person_seen")
        if isinstance(seen, int | float) and row.started_at <= seen <= row.started_at + 3600:
            return float(seen)
    return row.started_at


def window_around(moment: float, config: VidetteConfig) -> Window:
    cross = config.cross_camera
    return Window(moment, moment - cross.seconds("before"), moment + cross.seconds("after"))


def _named_start(path: Path) -> float | None:
    match = _STAMP.search(path.stem)
    return None if match is None else stamp_from_name(match.group(0))


def clips_between(
    folder: ClipFolderConfig, start: float, end: float, *, lag_s: float = 0.0
) -> list[tuple[Path, float, float]]:
    """Clips of `folder` that may show a moment in [start, end]: (path, start, end) by
    their stamps. With `lag_s`, a clip stamped up to that much after `end` still counts
    (its video arrived late, so it shows earlier moments than its stamp)."""
    found: list[tuple[Path, float, float]] = []
    for clip in find_clips(folder.path):
        named = _named_start(clip)
        if named is not None and not (start - STAMP_SLACK_S <= named <= end + lag_s + 60):
            continue
        try:
            incident = clip_incident(folder, clip)
        except (OSError, ValueError):
            continue
        began, ended = incident.started_at, incident.ended_at or incident.started_at
        if began - lag_s <= end and ended >= start:
            found.append((clip, began, ended))
    return sorted(found, key=lambda item: item[1])


def is_portrait(clip: Path) -> bool:
    """Taller than wide as shown: from a `vidette collect` sidecar (size and rotation);
    False when unknown."""
    meta = load_meta(clip.with_suffix(".json"))
    if meta is None or not meta.width or not meta.height:
        return False
    tall = meta.height > meta.width
    return not tall if meta.rotation in (90, 270) else tall


def tiles_for(linked: Linked) -> list[Tile]:
    """The mosaic's tiles: one per camera, the event's own camera first."""
    tiles: dict[str, Tile] = {}
    for clip in linked.clips:
        tile = tiles.get(clip.camera)
        if tile is None:
            tile = tiles[clip.camera] = Tile(clip.camera, portrait=is_portrait(clip.path))
        tile.pieces.append(Piece(clip.path, clip.started_at, clip.ended_at))
    return list(tiles.values())


def _faces(intent: Mapping[str, Any] | None) -> tuple[set[int], set[str]]:
    """Identity numbers and enrolled people's ids in an event's face report."""
    people = (intent or {}).get("people")
    seen = people.get("seen") if isinstance(people, Mapping) else None
    identities: set[int] = set()
    known: set[str] = set()
    for face in seen if isinstance(seen, list) else []:
        if not isinstance(face, Mapping):
            continue
        brief = face.get("identity")
        if isinstance(brief, Mapping) and isinstance(brief.get("id"), int):
            identities.add(int(brief["id"]))
        if face.get("status") == "known" and isinstance(face.get("person"), str):
            known.add(str(face["person"]))
    return identities, known


def _verdict(intent: Mapping[str, Any] | None) -> tuple[str | None, int | None]:
    verdicts = (intent or {}).get("verdicts")
    if not isinstance(verdicts, list) or not verdicts or not isinstance(verdicts[0], Mapping):
        return None, None
    first = verdicts[0]
    label = first.get("label") if isinstance(first.get("label"), str) else None
    persons = first.get("persons") if isinstance(first.get("persons"), int) else None
    return label, persons


class EventSource(Protocol):
    """The slice of `Database` this module reads."""

    async def get_event_by_source(self, source: str, source_ref: str) -> EventRow | None: ...


async def find_linked(
    row: EventRow,
    config: VidetteConfig,
    db: EventSource,
    *,
    at: float | None = None,
    cameras: Iterable[str] | None = None,
) -> Linked:
    """Every clip-folder clip around the event's moment (or `at`), the event's own clip
    first. `cameras` limits the search to those cameras. `db` answers
    `get_event_by_source` (the event database)."""
    window = window_around(at if at is not None else moment_of(row), config)
    wanted = None if cameras is None else set(cameras)
    folders = [f for f in config.clip_folders if wanted is None or f.camera in wanted]
    own_path = Path(row.clip_path).resolve() if row.clip_path else None
    my_ids, my_people = _faces(row.intent)
    clips: list[LinkedClip] = []
    for folder in folders:
        lag = config.cross_camera.lag_s(folder.camera)
        found = await asyncio.to_thread(clips_between, folder, window.start, window.end, lag_s=lag)
        for path, began, ended in found:
            own = own_path is not None and path.resolve() == own_path
            linked = LinkedClip(
                camera=folder.camera, path=path, started_at=began, ended_at=ended, own=own,
                approximate=lag > 0,
            )  # fmt: skip
            source_ref = (folder.path / path.relative_to(folder.path)).as_posix()
            event = await db.get_event_by_source(SOURCE, source_ref)
            if event is not None:
                linked.event_id = event.id
                linked.scenario, linked.persons = _verdict(event.intent)
                linked.identities, linked.people = _faces(event.intent)
            if not own:
                linked.same_person = bool(
                    (my_ids & linked.identities) or (my_people & linked.people)
                )
            clips.append(linked)
    if own_path is not None and not any(c.own for c in clips) and own_path.is_file():
        clips.append(
            LinkedClip(row.camera, own_path, row.started_at, row.ended_at or row.started_at,
                       own=True, approximate=config.cross_camera.lag_s(row.camera) > 0)
        )  # fmt: skip
    clips.sort(key=lambda c: (not c.own, c.started_at))
    return Linked(row.id, row.camera, window, clips)


async def find_around(
    moment: float,
    config: VidetteConfig,
    db: EventSource,
    *,
    cameras: Iterable[str] | None = None,
) -> Linked:
    """Every clip-folder clip around a moment, with no event to start from (a question
    like "what did the cameras record at 12:01?"). Nothing is "the same person" here."""
    nobody = EventRow(
        id="", camera="", started_at=moment, ended_at=moment, state="observed", kinds=[],
        zones=[], geometry={}, summary=None, intent=None, policy=None, feedback=None,
        snapshot_path=None, clip_path=None,
    )  # fmt: skip
    return await find_linked(nobody, config, db, at=moment, cameras=cameras)


def clock_text(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def describe(
    linked: Linked, *, camera_name: Callable[[str], str], configured: Iterable[str]
) -> list[str]:
    """Plain lines, in the bot's language, about what each camera recorded at that moment.
    A late camera's clip that only might show the moment is left out when the same camera
    has one that overlaps the window by its stamps."""
    lines: list[str] = []
    inside = {c.camera for c in linked.clips if c.overlap(linked.window) > 0}
    for clip in linked.clips:
        if clip.overlap(linked.window) <= 0 and clip.camera in inside:
            continue
        name = camera_name(clip.camera)
        span = f"{clock_text(clip.started_at)}–{clock_text(clip.ended_at)}"
        if clip.approximate:
            span = t("linked.around", span=span)
        parts = [f"{name}: {span}"]
        if clip.own:
            parts.append(t("linked.this_event"))
        elif clip.same_person:
            parts.append(t("linked.same_person"))
        elif clip.persons:
            parts.append(t("linked.person_no_face"))
        lines.append(", ".join(parts))
    silent = linked.silent_cameras(configured)
    if silent:
        names = ", ".join(camera_name(c) for c in silent)
        lines.append(t("linked.nothing", names=names))
    return lines


MOSAIC_DIR = "linked"  # under storage.media_dir
MOSAIC_KEEP_S = 86400.0  # stitched videos are removed a day after they were made


def _prune(folder: Path, now: float) -> None:
    for old in folder.glob("*.mp4"):
        with contextlib.suppress(OSError):
            if now - old.stat().st_mtime > MOSAIC_KEEP_S:
                old.unlink()


async def mosaic(
    linked: Linked, config: VidetteConfig, *, now: float | None = None
) -> Stitched | None:
    """The cameras at the event's moment as one video (or its parts), made on request
    under `media_dir/linked/` (2 to 3 s for a minute of three cameras on an Apple-silicon
    Mac) and removed a day later. None when only the event's own camera recorded anything
    in the window. Raises MediaToolError when ffmpeg is missing or fails."""
    import time

    tiles = tiles_for(linked)
    if len([t for t in tiles if t.pieces]) < 2:
        return None
    folder = config.storage.media_dir / MOSAIC_DIR
    folder.mkdir(parents=True, exist_ok=True)
    stamp = "|".join(f"{c.path}:{c.started_at:.2f}:{c.ended_at:.2f}" for c in linked.clips)
    key = hashlib.sha256(f"{stamp}|{linked.window}".encode()).hexdigest()[:12]
    out = folder / f"{linked.event_id}-{key}.mp4"
    await asyncio.to_thread(_prune, folder, now if now is not None else time.time())
    return await stitch(
        tiles, linked.window.start, linked.window.end, out,
        max_mb=config.cross_camera.max_video_mb,
    )  # fmt: skip
