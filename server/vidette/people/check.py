"""The face check: frames of an incident in, a `PeopleReport` out; and confirmations back.

Frames: a clip is sampled at `fps` (2 by default, at most `max_frames`) at full
resolution, because faces on a security camera are small and the key frames the VLM sees
are shrunk to 1280 px; without a clip, the key frames on disk are used as they are.

Grouping: readable faces that match a person are grouped by that person; faces that
match nobody are grouped by likeness (one stranger walking through ten frames is one
unknown face, not ten), and an unmatched face that looks like a face already matched in
the same clip joins that person instead of counting as a stranger.

Confirmations ("this is Sam", "this is a stranger", "not sure") come from the web app,
`vidette people confirm` or a contact-sheet reply ("5 Robin"). "This is Sam" adds the
crop as one of Sam's references; "a stranger" on a face that had matched someone raises
that person's threshold above the wrong score and stores nothing about the stranger.

Anonymous identities (ADR-0014): a check built with an `IdentityStore` (only `vidette
serve` and `vidette people identities backfill` build one) also matches each unknown
group against the identities seen before and records the sighting; the seen person
carries the identity's brief. "This is Sam" on such a face promotes the whole identity.
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

from vidette.ffmpeg import MediaToolError, sample_frames
from vidette.people.engine import DetectedFace, FaceEngine
from vidette.people.identities import (
    GroupFace,
    IdentityError,
    IdentityStore,
    SightingContext,
    promote,
)
from vidette.people.report import STRANGER, UNSURE, IdentityBrief, PeopleReport, SeenPerson
from vidette.people.store import Gallery, Match, NewFace, PeopleStore, StoreError
from vidette.people.vectors import Vector, cosine
from vidette.verdicts.base import Case

logger = logging.getLogger(__name__)

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv"})
SAME_FACE = 0.45  # two unmatched faces this alike are counted as one person
MAX_SEEN = 12

CropWriter = Callable[[int, bytes], str]  # (seen number, JPEG) -> path stored in the report
# (an unknown person's readable faces, their best score for anyone enrolled) -> identity
IdentityAssigner = Callable[[list[GroupFace], float], IdentityBrief | None]


@dataclass(frozen=True)
class CheckSettings:
    fps: float = 2.0
    max_frames: int = 24
    max_side: int = 1920  # frames sampled from a clip; faces need every pixel


@dataclass
class _Obs:
    frame: int
    face: DetectedFace
    match: Match
    embedding: Vector


@dataclass
class _Group:
    person: str | None
    name: str | None
    obs: list[_Obs] = field(default_factory=list)


def build_report(
    frames: list[list[DetectedFace]],
    gallery: Gallery,
    *,
    engine: str,
    threshold: float,
    crops: CropWriter | None = None,
    now: datetime | None = None,
    identities: IdentityAssigner | None = None,
) -> PeopleReport:
    """Group the faces found in each frame into the people seen in the clip; with
    `identities`, match each unknown person against the anonymous identities."""
    observations: list[_Obs] = []
    readable_frames = 0
    unclear_frames = 0
    for index, faces in enumerate(frames):
        readable = [f for f in faces if f.readable and f.embedding is not None]
        if readable:
            readable_frames += 1
        elif faces:
            unclear_frames += 1
        for face in readable:
            assert face.embedding is not None
            observations.append(_Obs(index, face, gallery.match(face.embedding), face.embedding))

    known: dict[str, _Group] = {}
    for obs in observations:
        if obs.match.person is not None:
            group = known.setdefault(obs.match.person, _Group(obs.match.person, obs.match.name))
            group.obs.append(obs)
    unknown: list[_Group] = []
    for obs in observations:
        if obs.match.person is not None:
            continue
        home = next(
            (
                group
                for group in known.values()
                if any(cosine(obs.embedding, o.embedding) >= SAME_FACE for o in group.obs)
            ),
            None,
        )
        if home is None:
            home = next(
                (
                    group
                    for group in unknown
                    if any(cosine(obs.embedding, o.embedding) >= SAME_FACE for o in group.obs)
                ),
                None,
            )
        if home is None:
            home = _Group(None, None)
            unknown.append(home)
        home.obs.append(obs)

    seen: list[SeenPerson] = []
    ordered = sorted(
        known.values(),
        key=lambda g: -max(o.match.score for o in g.obs if o.match.person == g.person),
    ) + sorted(unknown, key=lambda g: -len(g.obs))
    for group in ordered[:MAX_SEEN]:
        number = len(seen) + 1
        if group.person is not None:
            own = [o for o in group.obs if o.match.person == group.person]
            best = max(own, key=lambda o: o.match.score)
            score = best.match.score
        else:
            best = max(group.obs, key=lambda o: (o.face.face_px, o.face.score))
            score = max(o.match.score for o in group.obs)
        closest = None
        if group.person is None:
            nearest = max(group.obs, key=lambda o: o.match.score)
            closest = nearest.match.closest
        crop = None
        if crops is not None and best.face.crop is not None:
            crop = crops(number, best.face.crop)
        identity = None
        if group.person is None and identities is not None:
            group_faces = [
                GroupFace(o.embedding, o.face.face_px, o.face.score, o.face.crop) for o in group.obs
            ]
            identity = identities(group_faces, score)
        seen.append(
            SeenPerson(
                n=number,
                status="known" if group.person is not None else "unknown",
                person=group.person,
                name=group.name,
                score=round(score, 3),
                closest=closest,
                frames=sorted({o.frame for o in group.obs}),
                faces=len(group.obs),
                face_px=max(o.face.face_px for o in group.obs),
                crop=crop,
                identity=identity,
            )
        )
    return PeopleReport(
        checked_at=now or datetime.now().astimezone().replace(microsecond=0),
        engine=engine,
        threshold=threshold,
        enrolled=len(gallery),
        frames_checked=len(frames),
        frames_with_readable_faces=readable_frames,
        unclear_frames=unclear_frames,
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


class PeopleCheck:
    """The face check for one people folder and one engine (reusable, sequential).
    With `identities`, unknown faces are also tracked as anonymous identities."""

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

    async def frame_images(self, case: Case) -> list[bytes]:
        """Frames sampled from the clip, or the images on hand when there is no clip or
        no ffmpeg (the `vidette serve` image ships without it)."""
        images = [p for p in case.media if p.suffix.lower() in IMAGE_SUFFIXES and p.is_file()]
        videos = [p for p in case.media if p.suffix.lower() in VIDEO_SUFFIXES and p.is_file()]
        if videos:
            with tempfile.TemporaryDirectory(prefix="vidette-faces-") as tmp:
                try:
                    paths = await sample_frames(
                        videos[0],
                        Path(tmp),
                        fps=self.settings.fps,
                        max_frames=self.settings.max_frames,
                        max_side=self.settings.max_side,
                    )
                except MediaToolError:
                    if not images:
                        raise
                else:
                    return [path.read_bytes() for path in paths]
        return [path.read_bytes() for path in images[: self.settings.max_frames]]

    def analyse(
        self,
        images: list[bytes],
        crops: CropWriter | None = None,
        context: SightingContext | None = None,
    ) -> PeopleReport:
        """Blocking: run in a worker thread. `context` (with an identity store) records
        the unknown faces as sightings of anonymous identities."""
        frames: list[list[DetectedFace]] = []
        for image in images:
            try:
                frames.append(self.engine.faces(image))
            except ValueError:
                frames.append([])
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
        )

    @staticmethod
    def _assigner(store: IdentityStore, context: SightingContext) -> IdentityAssigner:
        def assign(faces: list[GroupFace], known_score: float) -> IdentityBrief | None:
            try:
                return store.assign(faces, context, known_score=known_score).brief
            except (IdentityError, sqlite3.Error, OSError) as exc:  # never fails the check
                logger.warning("identity store: %s", exc)
                return None

        return assign

    async def check(self, case: Case, crops: CropWriter | None = None) -> PeopleReport:
        images = await self.frame_images(case)
        return await asyncio.to_thread(self.analyse, images, crops, sighting_context(case))


@dataclass(frozen=True)
class Confirmation:
    seen: SeenPerson
    answer: str  # a person id, "stranger" or "unsure"
    message: str


def confirm(
    report: PeopleReport,
    n: int,
    answer: str,
    *,
    store: PeopleStore,
    engine: FaceEngine | None,
    media_root: Path,
    where: str,
    identities: IdentityStore | None = None,
) -> Confirmation:
    """Apply one answer to seen person `n` and mark it in `report` (the caller saves the
    report). `answer`: a person's name or id, "stranger" or "unsure". With `identities`,
    "this is Sam" on a face that belongs to an anonymous identity promotes the identity:
    its faces from every sighting become Sam's references too."""
    seen = report.get(n)
    if seen is None:
        raise StoreError(f"there is no face {n} here (faces: {len(report.seen)})")
    word = answer.strip().lower()
    message: str
    if word == UNSURE:
        result = UNSURE
        message = "noted as unsure; nothing learned"
    elif word == STRANGER:
        result = STRANGER
        if seen.status == "known" and seen.person is not None:
            raised = store.reject(seen.person, seen.score, where)
            message = (
                f"noted: not {seen.name}; {seen.name}'s threshold is now {raised:.2f} "
                "(nothing about this face is kept in the people folder)"
            )
        else:
            message = "noted as a stranger; nothing about this face is kept"
    else:
        person = store.require(answer)
        if seen.crop is None:
            raise StoreError(f"face {n} has no crop to learn from")
        if engine is None:
            raise StoreError("face recognition is not available here (models or OpenCV)")
        path = (media_root / seen.crop).resolve()
        if not path.is_relative_to(media_root.resolve()) or not path.is_file():
            raise StoreError(f"the crop for face {n} is gone ({seen.crop})")
        crop = path.read_bytes()
        added = store.add_faces(
            person.id,
            [NewFace(engine.embed_crop(crop), crop, seen.face_px, f"confirmed:{where}#{n}")],
            kind="confirmed",
            check_outliers=False,
        )
        if seen.status == "known" and seen.person not in (None, person.id):
            assert seen.person is not None
            store.reject(seen.person, seen.score, where)
        result = person.id
        promoted = ""
        if identities is not None and seen.identity is not None:
            try:
                _row, more = promote(identities, seen.identity.id, store, person.id)
            except IdentityError:
                pass  # deleted or merged since: the face itself is still learned
            else:
                promoted = f"; stranger #{seen.identity.id} is {person.name} now"
                if more:
                    promoted += f" ({more} more face(s) from earlier clips)"
        if added.added:
            message = f"added to {person.name}'s references"
        elif added.duplicates:
            message = f"{person.name} already has this face; nothing added"
        elif added.elsewhere:
            _source, other, score = added.elsewhere[0]
            message = f"not added: it looks more like {other} ({score:.2f})"
        else:
            message = f"not added: {person.name} has {store.max_faces} faces already"
        message += promoted
    seen.confirmed = result
    seen.confirmed_at = datetime.now().astimezone().replace(microsecond=0)
    return Confirmation(seen=seen, answer=result, message=message)
