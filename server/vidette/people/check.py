"""The face check: frames of an incident in, a `PeopleReport` out.

Frames: a clip is sampled at `fps` (2 by default, at most `max_frames`) at full
resolution, because faces on a security camera are small and the key frames the VLM sees
are shrunk to 1280 px; without a clip, the key frames on disk are used as they are.
Wherever a face was found, the clip is looked at again around it, `extra_fps` (6) frames
a second for at most `extra_frames` (24) more frames (ADR-0016): a walking face is sharp
for a moment between blurred ones, and that moment is the one worth keeping.

Grouping: readable faces that match a person are grouped by that person; faces that
match nobody are grouped by likeness (one stranger walking through ten frames is one
unknown face, not ten), and an unmatched face that looks like a face already matched in
the same clip joins that person instead of counting as a stranger. Faces too small or
blurred to compare are followed from frame to frame by where they are (a face near the
same place a moment later is the same person): next to a readable face they are that
person; on their own, seen in at least two frames, they are an *unclear* person.

Each person's picture (the crop kept with the report, the crop and embeddings an
identity keeps) is the best moment: detector confidence × sharpness × size ×
frontalness (`engine.moment_score`), readable faces first.

Anonymous identities (ADR-0014, ADR-0016): a check built with an `IdentityStore` (only
`vidette serve` and `vidette people identities backfill` build one) gives every person
it saw who is not enrolled an identity: unknown faces join the one they clearly match or
start one, unclear faces start a weak one. Decisions about faces afterwards:
`vidette.people.decisions` (re-exported here as `confirm`).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from vidette.ffmpeg import MediaToolError, binary, probe, run, scale_filter
from vidette.people.decisions import Confirmation, confirm
from vidette.people.engine import DetectedFace, FaceEngine, moment_score
from vidette.people.identities import GroupFace, IdentityError, IdentityStore, SightingContext
from vidette.people.report import IdentityBrief, PeopleReport, SeenPerson
from vidette.people.store import Gallery, Match, PeopleStore
from vidette.people.vectors import Vector, cosine
from vidette.verdicts.base import Case

logger = logging.getLogger(__name__)

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv"})
SAME_FACE = 0.45  # two unmatched faces this alike are counted as one person
MAX_SEEN = 12
TRACK_GAP_S = 0.8  # faces this close in time (or adjacent frames without times)...
TRACK_DISTANCE = 2.5  # ...whose centres are within this many face widths: one person
MIN_UNCLEAR_FACES = 2  # an unclear person must be seen in two frames (detector noise)

CropWriter = Callable[[int, bytes], str]  # (seen number, JPEG) -> path stored in the report
# (a person's faces, their best score for anyone enrolled, weak) -> the identity
IdentityAssigner = Callable[[list[GroupFace], float, bool], IdentityBrief | None]

__all__ = [
    "CheckSettings",
    "Confirmation",
    "CropWriter",
    "PeopleCheck",
    "build_report",
    "confirm",
    "crop_writer",
    "sighting_context",
    "windows_around",
]


@dataclass(frozen=True)
class CheckSettings:
    fps: float = 2.0
    max_frames: int = 24
    max_side: int = 1920  # frames sampled from a clip; faces need every pixel
    extra_fps: float = 6.0  # around faces, for the best moment (ADR-0016)
    extra_frames: int = 24  # at most this many more frames a clip; 0 turns it off
    extra_pad_s: float = 0.6  # seconds before and after each frame with a face


@dataclass
class _Obs:
    frame: int
    t: float | None
    face: DetectedFace
    match: Match | None  # readable faces only
    readable: bool

    @property
    def embedding(self) -> Vector:
        assert self.face.embedding is not None
        return self.face.embedding


@dataclass
class _Group:
    person: str | None
    name: str | None
    obs: list[_Obs] = field(default_factory=list)


def _near(a: _Obs, b: _Obs) -> float | None:
    """How far apart two faces are, in face widths, when they can be one person moving;
    None when they cannot (the same frame, too far apart in time or space)."""
    if a.frame == b.frame:
        return None
    if a.t is not None and b.t is not None:
        if abs(a.t - b.t) > TRACK_GAP_S:
            return None
    elif abs(a.frame - b.frame) > 1:
        return None
    ax, ay, aw, ah = a.face.box
    bx, by, bw, bh = b.face.box
    width = max(aw, bw, 1)
    dx = (ax + aw / 2) - (bx + bw / 2)
    dy = (ay + ah / 2) - (by + bh / 2)
    distance = float((dx * dx + dy * dy) ** 0.5) / width
    return distance if distance <= TRACK_DISTANCE else None


def _own_score(group: _Group) -> float:
    return max(
        o.match.score for o in group.obs if o.match is not None and o.match.person == group.person
    )


def build_report(
    frames: list[list[DetectedFace]],
    gallery: Gallery,
    *,
    engine: str,
    threshold: float,
    crops: CropWriter | None = None,
    now: datetime | None = None,
    identities: IdentityAssigner | None = None,
    times: list[float | None] | None = None,
    extra: list[bool] | None = None,
) -> PeopleReport:
    """Group the faces found in each frame into the people seen in the clip; with
    `identities`, give each one who is not enrolled an anonymous identity. `times`: each
    frame's time in the clip; `extra`: frames looked at again around faces, left out of
    the frame counts."""
    times = times if times is not None and len(times) == len(frames) else [None] * len(frames)
    extra = extra if extra is not None and len(extra) == len(frames) else [False] * len(frames)
    observations: list[_Obs] = []
    readable_frames = unclear_frames = 0
    for index, faces in enumerate(frames):
        readable = [f for f in faces if f.readable and f.embedding is not None]
        if not extra[index]:
            if readable:
                readable_frames += 1
            elif faces:
                unclear_frames += 1
        for face in faces:
            if face.readable and face.embedding is not None:
                match = gallery.match(face.embedding)
                observations.append(_Obs(index, times[index], face, match, True))
            else:
                observations.append(_Obs(index, times[index], face, None, False))

    known: dict[str, _Group] = {}
    for obs in observations:
        if obs.match is not None and obs.match.person is not None:
            group = known.setdefault(obs.match.person, _Group(obs.match.person, obs.match.name))
            group.obs.append(obs)
    unknown: list[_Group] = []
    for obs in observations:
        if not obs.readable or (obs.match is not None and obs.match.person is not None):
            continue

        def alike(group: _Group, obs: _Obs = obs) -> bool:
            return any(
                o.readable and cosine(obs.embedding, o.embedding) >= SAME_FACE for o in group.obs
            )

        home = next((g for g in known.values() if alike(g)), None)
        home = home or next((g for g in unknown if alike(g)), None)
        if home is None:
            home = _Group(None, None)
            unknown.append(home)
        home.obs.append(obs)
    unclear: list[_Group] = []
    for obs in sorted(
        (o for o in observations if not o.readable),
        key=lambda o: (o.t if o.t is not None else float(o.frame), o.frame),
    ):
        nearest: tuple[float, _Group] | None = None
        for group in [*known.values(), *unknown, *unclear]:
            for other in group.obs:
                distance = _near(obs, other)
                if distance is not None and (nearest is None or distance < nearest[0]):
                    nearest = (distance, group)
        if nearest is None:
            unclear.append(_Group(None, None, [obs]))
        else:
            nearest[1].obs.append(obs)
    unclear = [g for g in unclear if len({o.frame for o in g.obs}) >= MIN_UNCLEAR_FACES]

    seen: list[SeenPerson] = []
    ordered = (
        sorted(known.values(), key=lambda g: -_own_score(g))
        + sorted(unknown, key=lambda g: -len(g.obs))
        + sorted(unclear, key=lambda g: -len(g.obs))
    )
    for group in ordered[:MAX_SEEN]:
        number = len(seen) + 1
        readable_obs = [o for o in group.obs if o.readable]
        best = max(readable_obs or group.obs, key=lambda o: moment_score(o.face))
        closest = None
        status: str
        if group.person is not None:
            score, status = _own_score(group), "known"
        elif readable_obs:
            nearest_obs = max(readable_obs, key=lambda o: o.match.score if o.match else 0.0)
            score = nearest_obs.match.score if nearest_obs.match else 0.0
            closest = nearest_obs.match.closest if nearest_obs.match else None
            status = "unknown"
        else:
            score, status = 0.0, "unclear"
        crop = None
        if crops is not None and best.face.crop is not None:
            crop = crops(number, best.face.crop)
        identity = None
        if group.person is None and identities is not None:
            source = readable_obs or [o for o in group.obs if o.face.embedding is not None]
            group_faces = [
                GroupFace(o.embedding, o.face.face_px, o.face.score, o.face.crop,
                          quality=moment_score(o.face))
                for o in source
            ]  # fmt: skip
            if group_faces:
                identity = identities(group_faces, score, not readable_obs)
        seen.append(
            SeenPerson.model_validate(
                {
                    "n": number,
                    "status": status,
                    "person": group.person,
                    "name": group.name,
                    "score": round(score, 3),
                    "closest": closest,
                    "frames": sorted({o.frame for o in group.obs}),
                    "faces": len(group.obs),
                    "face_px": max(o.face.face_px for o in group.obs),
                    "crop": crop,
                    "identity": identity,
                    "best_at_s": None if best.t is None else round(best.t, 2),
                }
            )
        )
    return PeopleReport(
        checked_at=now or datetime.now().astimezone().replace(microsecond=0),
        engine=engine,
        threshold=threshold,
        enrolled=len(gallery),
        frames_checked=sum(1 for e in extra if not e),
        frames_with_readable_faces=readable_frames,
        unclear_frames=unclear_frames,
        extra_frames=sum(1 for e in extra if e),
        seen=seen,
    )


def crop_writer(root: Path, folder: str) -> CropWriter:
    """Crops go to `root/folder/<n>.jpg` (0600, folder 0700); the report keeps
    `folder/<n>.jpg`, relative to `root`."""
    from vidette.paths import write_private

    def write(number: int, data: bytes) -> str:
        relative = f"{folder}/{number}.jpg"
        write_private(root / relative, data)
        return relative

    return write


def sighting_context(case: Case) -> SightingContext:
    """Where and when the faces of a case were seen, for the identity store."""
    incident = case.incident
    clip = next((p for p in case.media if p.suffix.lower() in VIDEO_SUFFIXES), None)
    return SightingContext(
        at=incident.started_at,
        camera=incident.camera,
        source=incident.source,
        source_id=incident.source_id,
        clip=None if clip is None else str(clip),
    )


def windows_around(
    times: list[float], *, pad: float, duration: float | None = None
) -> list[tuple[float, float]]:
    """Merged [start, end] windows of `pad` seconds around each time, within the clip."""
    spans = sorted(
        (max(0.0, t - pad), t + pad if duration is None else min(duration, t + pad)) for t in times
    )
    merged: list[tuple[float, float]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return [(s, e) for s, e in merged if e > s]


async def _ffmpeg_frames(
    video: Path,
    out_dir: Path,
    prefix: str,
    *,
    rate: float,
    count: int,
    max_side: int,
    start: float | None = None,
    length: float | None = None,
) -> list[tuple[float, Path]]:
    """Frames at `rate` per second (from `start` for `length` seconds), with their times."""
    out_dir.mkdir(parents=True, exist_ok=True)
    seek = [] if start is None else ["-ss", f"{start:.3f}", "-t", f"{length or 0:.3f}"]
    rc, _, stderr = await run(
        [
            binary("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *seek,
            "-i", str(video), "-vf", f"fps={rate:.4f},{scale_filter(max_side)}",
            "-frames:v", str(count), "-q:v", "2", str(out_dir / f"{prefix}%03d.jpg"),
        ],
        timeout=120,
    )  # fmt: skip
    paths = sorted(out_dir.glob(f"{prefix}*.jpg"))
    if rc != 0 and not paths:
        raise MediaToolError(f"cannot sample frames from {video.name}: {stderr.strip()[-200:]}")
    base = start or 0.0
    return [(base + index / rate, path) for index, path in enumerate(paths)]


class PeopleCheck:
    """The face check for one people folder and one engine (reusable, sequential).
    With `identities`, people who are not enrolled are tracked as anonymous identities."""

    def __init__(
        self,
        store: PeopleStore,
        engine: FaceEngine,
        settings: CheckSettings | None = None,
        identities: IdentityStore | None = None,
    ) -> None:
        self.store = store
        self.engine = engine
        self.settings = settings or CheckSettings()
        self.identities = identities

    async def timed_images(self, case: Case, work: Path) -> list[tuple[float | None, bytes]]:
        """Frames sampled from the clip with their times, or the images on hand when
        there is no clip or no ffmpeg (the `vidette serve` image ships without it)."""
        images = [p for p in case.media if p.suffix.lower() in IMAGE_SUFFIXES and p.is_file()]
        videos = [p for p in case.media if p.suffix.lower() in VIDEO_SUFFIXES and p.is_file()]
        if videos:
            try:
                info = await probe(videos[0])
                if info.duration_s > 0 and self.settings.max_frames > 0:
                    rate = min(self.settings.fps, self.settings.max_frames / info.duration_s)
                    frames = await _ffmpeg_frames(
                        videos[0], work, "c", rate=rate, count=self.settings.max_frames,
                        max_side=self.settings.max_side,
                    )  # fmt: skip
                    return [(t, path.read_bytes()) for t, path in frames]
            except MediaToolError:
                if not images:
                    raise
        return [(None, path.read_bytes()) for path in images[: self.settings.max_frames]]

    async def frame_images(self, case: Case) -> list[bytes]:
        with tempfile.TemporaryDirectory(prefix="vidette-faces-") as tmp:
            return [image for _t, image in await self.timed_images(case, Path(tmp))]

    async def extra_images(
        self, video: Path, face_times: list[float], work: Path
    ) -> list[tuple[float | None, bytes]]:
        """More frames around the faces found, for their best moment (ADR-0016)."""
        settings = self.settings
        if settings.extra_frames <= 0 or not face_times:
            return []
        windows = windows_around(face_times, pad=settings.extra_pad_s)
        span = sum(end - start for start, end in windows)
        rate = min(settings.extra_fps, settings.extra_frames / max(span, 0.1))
        out: list[tuple[float | None, bytes]] = []
        for index, (start, end) in enumerate(windows):
            budget = settings.extra_frames - len(out)
            if budget <= 0:
                break
            frames = await _ffmpeg_frames(
                video, work, f"x{index}-", rate=rate, count=budget, max_side=settings.max_side,
                start=start, length=end - start,
            )  # fmt: skip
            out += [(t, path.read_bytes()) for t, path in frames]
        return out

    def _detect(self, images: list[bytes]) -> list[list[DetectedFace]]:
        frames: list[list[DetectedFace]] = []
        for image in images:
            try:
                frames.append(self.engine.faces(image))
            except ValueError:
                frames.append([])
        return frames

    def _report(
        self,
        frames: list[list[DetectedFace]],
        crops: CropWriter | None,
        context: SightingContext | None,
        times: list[float | None] | None = None,
        extra: list[bool] | None = None,
    ) -> PeopleReport:
        assigner = None
        if self.identities is not None and context is not None:
            assigner = self._assigner(self.identities, context)
        return build_report(
            frames,
            self.store.gallery(),
            engine=self.engine.name,
            threshold=self.store.threshold,
            crops=crops,
            identities=assigner,
            times=times,
            extra=extra,
        )

    def analyse(
        self,
        images: list[bytes],
        crops: CropWriter | None = None,
        context: SightingContext | None = None,
    ) -> PeopleReport:
        """Blocking, the frames as given (no second look): run in a worker thread."""
        return self._report(self._detect(images), crops, context)

    @staticmethod
    def _assigner(store: IdentityStore, context: SightingContext) -> IdentityAssigner:
        def assign(faces: list[GroupFace], known_score: float, weak: bool) -> IdentityBrief | None:
            try:
                return store.assign(faces, context, known_score=known_score, weak=weak).brief
            except (IdentityError, sqlite3.Error, OSError) as exc:  # never fails the check
                logger.warning("identity store: %s", exc)
                return None

        return assign

    async def check(self, case: Case, crops: CropWriter | None = None) -> PeopleReport:
        with tempfile.TemporaryDirectory(prefix="vidette-faces-") as tmp:
            work = Path(tmp)
            timed = await self.timed_images(case, work)
            frames = await asyncio.to_thread(self._detect, [image for _t, image in timed])
            times: list[float | None] = [t for t, _image in timed]
            extra = [False] * len(frames)
            video = next(
                (p for p in case.media if p.suffix.lower() in VIDEO_SUFFIXES and p.is_file()),
                None,
            )
            face_times = [
                t for t, found in zip(times, frames, strict=True) if found and t is not None
            ]
            if video is not None and face_times:
                try:
                    more = await self.extra_images(video, face_times, work)
                except MediaToolError as exc:
                    logger.warning("second look at %s failed: %s", video.name, exc)
                    more = []
                if more:
                    more_frames = await asyncio.to_thread(self._detect, [i for _t, i in more])
                    merged = sorted(
                        [
                            *zip(times, frames, extra, strict=True),
                            *((t, f, True) for (t, _i), f in zip(more, more_frames, strict=True)),
                        ],
                        key=lambda item: (item[0] if item[0] is not None else 0.0, item[2]),
                    )
                    times = [t for t, _f, _e in merged]
                    frames = [f for _t, f, _e in merged]
                    extra = [e for _t, _f, e in merged]
        return await asyncio.to_thread(
            self._report, frames, crops, sighting_context(case), times, extra
        )
