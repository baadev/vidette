"""The people folder: who is enrolled, their reference faces and what was confirmed.

    <people.dir>/                    0700, never inside a git work tree
      sam/
        person.json                  name, created, wrong matches confirmed later
        faces.json                   one entry per reference face: embedding, source, kind
        faces/<id>.jpg               the aligned 112×112 crop each embedding came from
        photos/<sha>.jpg             the photos given to `vidette people add` (or the UI)

Every file is 0600 and written atomically. A folder without `person.json` (photos
dropped in by hand) is listed as "not enrolled yet" and never matched.

Thresholds: a face matches a person at or above the global threshold; each time someone
confirms that a match was wrong ("this is not Sam"), that person's own threshold rises
just above the wrong score. Nothing about the stranger is stored: only the score.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import re
import shutil
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from vidette.paths import git_work_tree, private_dir, too_open, write_private
from vidette.people.vectors import Vector, cosine, decode, encode

PERSON_FILE = "person.json"
FACES_FILE = "faces.json"
FACES_DIR = "faces"
PHOTOS_DIR = "photos"
LOCK_FILE = ".lock"

DUPLICATE_SIMILARITY = 0.92  # a new face this close to an existing reference adds nothing
OUTLIER_SIMILARITY = 0.25  # a face this unlike all the others is probably someone else
REJECT_MARGIN = 0.02  # a person's threshold rises this far above a confirmed wrong match
MAX_THRESHOLD = 0.95
AMBIGUOUS_MARGIN = 0.05  # two people this close to each other: neither is claimed

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
Kind = Literal["enrolled", "confirmed"]


class StoreError(Exception):
    """The people folder cannot be used as asked; the message says what to do."""


def person_id(name: str) -> str:
    """A folder name for a display name: "Sam" → sam, "Anna Maria" → anna-maria."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")[:40].strip("-")
    if not slug:
        slug = "person-" + hashlib.sha256(name.encode()).hexdigest()[:6]
    return slug


def default_people_dir() -> Path:
    import os

    base = os.environ.get("VIDETTE_DATA")
    return (Path(base) if base else Path("~/vidette-data")).expanduser() / "people"


class Rejection(BaseModel):
    score: float
    at: datetime
    where: str | None = None  # the clip or event the wrong match came from


class Person(BaseModel):
    version: Literal[1] = 1
    id: str
    name: str
    created_at: datetime
    rejected: list[Rejection] = Field(default_factory=list)


class FaceRecord(BaseModel):
    id: str
    file: str  # relative to the person's folder
    embedding: str  # base64 float32 (vectors.encode)
    kind: Kind
    source: str  # "photo:<photo file>#<n>" or "confirmed:<clip or event>"
    added_at: datetime
    face_px: int = 0


class FacesFile(BaseModel):
    version: Literal[1] = 1
    faces: list[FaceRecord] = Field(default_factory=list)


@dataclass(frozen=True)
class NewFace:
    embedding: Vector
    crop: bytes  # aligned JPEG
    face_px: int
    source: str


@dataclass
class AddResult:
    added: list[str] = field(default_factory=list)  # face ids
    duplicates: int = 0
    outliers: list[str] = field(default_factory=list)  # sources skipped as someone else
    elsewhere: list[tuple[str, str, float]] = field(default_factory=list)  # (source, id, s)
    dropped_for_room: int = 0  # older confirmed faces removed to make room
    refused_full: int = 0


@dataclass(frozen=True)
class PersonView:
    id: str
    name: str
    created_at: datetime
    enrolled: int
    confirmed: int
    photos: int
    threshold: float
    rejected: int


@dataclass(frozen=True)
class Candidate:
    id: str
    name: str
    threshold: float
    embeddings: tuple[Vector, ...]


@dataclass(frozen=True)
class Match:
    person: str | None  # the id when the face is this person; None otherwise
    name: str | None
    score: float  # the matched person's score, or the best score for anyone
    closest: str | None  # the nearest person's id, matched or not
    ambiguous: bool = False


@dataclass(frozen=True)
class Gallery:
    candidates: tuple[Candidate, ...]

    def __len__(self) -> int:
        return len(self.candidates)

    def match(self, embedding: Vector) -> Match:
        """The person this face is, by the best similarity to any of their references,
        if it clears their threshold and no other person is as close."""
        scored = sorted(
            (
                (max(cosine(embedding, ref) for ref in candidate.embeddings), candidate)
                for candidate in self.candidates
                if candidate.embeddings
            ),
            key=lambda item: -item[0],
        )
        if not scored:
            return Match(person=None, name=None, score=0.0, closest=None)
        best_score, best = scored[0]
        if best_score < best.threshold:
            return Match(person=None, name=None, score=best_score, closest=best.id)
        if len(scored) > 1:
            second_score, second = scored[1]
            if second_score >= second.threshold and best_score - second_score < AMBIGUOUS_MARGIN:
                return Match(
                    person=None, name=None, score=best_score, closest=best.id, ambiguous=True
                )
        return Match(person=best.id, name=best.name, score=best_score, closest=best.id)


def effective_threshold(person: Person, base: float) -> float:
    if not person.rejected:
        return base
    raised = max(rejection.score for rejection in person.rejected) + REJECT_MARGIN
    return round(min(MAX_THRESHOLD, max(base, raised)), 3)


def _now() -> datetime:
    return datetime.now().astimezone().replace(microsecond=0)


class PeopleStore:
    """One people folder. Every method re-reads the files: the CLI and the server may
    both write, and a lock file serialises their changes."""

    def __init__(
        self,
        root: Path,
        *,
        threshold: float = 0.4,
        max_faces: int = 60,
        allow_inside_git: bool = False,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.threshold = threshold
        self.max_faces = max_faces
        repo = git_work_tree(self.root)
        if repo is not None and not allow_inside_git:
            raise StoreError(
                f"{self.root} is inside the git work tree {repo}; faces are biometric data "
                "and must never be committed. Choose a folder outside any repository "
                "(people.dir, default ~/vidette-data/people)"
            )

    # --- files -------------------------------------------------------------------------

    def _dir(self, pid: str) -> Path:
        if not _ID_RE.match(pid):
            raise StoreError(f"'{pid}' is not a person id (letters, digits and '-')")
        return self.root / pid

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        private_dir(self.root)
        lock = self.root / LOCK_FILE
        with lock.open("a") as handle:
            lock.chmod(0o600)
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _read_person(self, pid: str) -> Person | None:
        path = self._dir(pid) / PERSON_FILE
        try:
            return Person.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, ValidationError) as exc:
            raise StoreError(f"{path} is damaged: {exc}") from exc

    def _write_person(self, person: Person) -> None:
        data = person.model_dump_json(indent=2) + "\n"
        write_private(self._dir(person.id) / PERSON_FILE, data.encode())

    def _read_faces(self, pid: str) -> FacesFile:
        path = self._dir(pid) / FACES_FILE
        try:
            return FacesFile.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return FacesFile()
        except (OSError, ValueError, ValidationError) as exc:
            raise StoreError(f"{path} is damaged: {exc}") from exc

    def _write_faces(self, pid: str, faces: FacesFile) -> None:
        data = faces.model_dump_json(indent=1) + "\n"
        write_private(self._dir(pid) / FACES_FILE, data.encode())

    # --- reading -----------------------------------------------------------------------

    def exists(self) -> bool:
        return self.root.is_dir()

    def permission_warnings(self) -> list[str]:
        """Folders or files others could read (fixed on the next write, but say so)."""
        if not self.root.is_dir():
            return []
        loose = [p for p in [self.root, *self.root.rglob("*")] if too_open(p)]
        if not loose:
            return []
        return [
            f"{len(loose)} item(s) under {self.root} are readable by other users (e.g. "
            f"{loose[0]}); run: chmod -R go-rwx {self.root}"
        ]

    def people(self) -> list[Person]:
        if not self.root.is_dir():
            return []
        found = []
        for child in sorted(self.root.iterdir()):
            if child.is_dir() and _ID_RE.match(child.name):
                person = self._read_person(child.name)
                if person is not None:
                    found.append(person)
        return found

    def not_enrolled(self) -> list[str]:
        """Sub-folders with no person.json: photos dropped in by hand, never matched."""
        if not self.root.is_dir():
            return []
        return [
            child.name
            for child in sorted(self.root.iterdir())
            if child.is_dir()
            and not child.name.startswith(".")
            and not (child / PERSON_FILE).exists()
        ]

    def find(self, name: str) -> Person | None:
        """By id or display name, ignoring case."""
        wanted = name.strip().lower()
        for person in self.people():
            if person.id == wanted or person.name.lower() == wanted:
                return person
        return None

    def require(self, name: str) -> Person:
        person = self.find(name)
        if person is None:
            known = ", ".join(p.name for p in self.people()) or "nobody yet"
            raise StoreError(f"no person '{name}' in {self.root} (enrolled: {known})")
        return person

    def faces(self, pid: str) -> list[FaceRecord]:
        return self._read_faces(pid).faces

    def face_path(self, pid: str, face_id: str) -> Path | None:
        for record in self.faces(pid):
            if record.id == face_id:
                path = (self._dir(pid) / record.file).resolve()
                if path.is_relative_to(self._dir(pid).resolve()) and path.is_file():
                    return path
        return None

    def view(self, person: Person) -> PersonView:
        faces = self.faces(person.id)
        photos = self._dir(person.id) / PHOTOS_DIR
        return PersonView(
            id=person.id,
            name=person.name,
            created_at=person.created_at,
            enrolled=sum(1 for f in faces if f.kind == "enrolled"),
            confirmed=sum(1 for f in faces if f.kind == "confirmed"),
            photos=len(list(photos.iterdir())) if photos.is_dir() else 0,
            threshold=effective_threshold(person, self.threshold),
            rejected=len(person.rejected),
        )

    def gallery(self) -> Gallery:
        return Gallery(
            tuple(
                Candidate(
                    id=person.id,
                    name=person.name,
                    threshold=effective_threshold(person, self.threshold),
                    embeddings=tuple(decode(f.embedding) for f in self.faces(person.id)),
                )
                for person in self.people()
            )
        )

    # --- changes -----------------------------------------------------------------------

    def create(self, name: str) -> Person:
        name = " ".join(name.split())
        if not name or len(name) > 40 or not name.isprintable():
            raise StoreError("a name is 1 to 40 printable characters")
        with self._locked():
            pid = person_id(name)
            existing = self._read_person(pid)
            if existing is not None:
                if existing.name != name:  # "sam" then "Sam": same person, new spelling
                    existing.name = name
                    self._write_person(existing)
                return existing
            person = Person(id=pid, name=name, created_at=_now())
            private_dir(self._dir(pid))
            self._write_person(person)
            return person

    def delete(self, name: str) -> Person:
        """Remove a person: every photo, crop and embedding the store keeps. Cannot be
        undone. Files someone put into the folder by hand are left where they are (the
        folder goes only when nothing else is in it)."""
        with self._locked():
            person = self.require(name)
            folder = self._dir(person.id)
            for managed in (FACES_DIR, PHOTOS_DIR):
                if (folder / managed).is_dir():
                    shutil.rmtree(folder / managed)
            for managed in (FACES_FILE, PERSON_FILE):
                (folder / managed).unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                folder.rmdir()  # only when empty
            return person

    def foreign_files(self, pid: str) -> list[str]:
        """Files in a person's folder that the store did not write (kept on delete)."""
        folder = self._dir(pid)
        managed = {FACES_DIR, PHOTOS_DIR, FACES_FILE, PERSON_FILE}
        if not folder.is_dir():
            return []
        return sorted(p.name for p in folder.iterdir() if p.name not in managed)

    def add_photo(self, pid: str, data: bytes, suffix: str) -> str:
        """Keep an original photo (named by its content); returns its file name."""
        suffix = suffix.lower() if suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") else ".jpg"
        name = hashlib.sha256(data).hexdigest()[:16] + suffix
        with self._locked():
            path = self._dir(pid) / PHOTOS_DIR / name
            if not path.exists():
                write_private(path, data)
        return name

    def add_faces(
        self, pid: str, faces: list[NewFace], *, kind: Kind, check_outliers: bool = True
    ) -> AddResult:
        """Add reference faces: duplicates are dropped, a face much closer to someone
        else is refused, and (for a batch of 3+) faces unlike all the rest are skipped
        as probably another person. Confirmed faces make room by replacing the oldest
        confirmed ones; enrolled faces stop at the limit."""
        result = AddResult()
        with self._locked():
            person = self._read_person(pid)
            if person is None:
                raise StoreError(f"no person '{pid}'; create it first")
            stored = self._read_faces(pid)
            others = [c for c in self.gallery().candidates if c.id != pid]
            own = [decode(record.embedding) for record in stored.faces]
            batch = [face.embedding for face in faces]
            for index, face in enumerate(faces):
                if any(cosine(face.embedding, ref) >= DUPLICATE_SIMILARITY for ref in own):
                    result.duplicates += 1
                    continue
                rivals = [
                    (max(cosine(face.embedding, ref) for ref in c.embeddings), c)
                    for c in others
                    if c.embeddings
                ]
                if rivals:
                    best_score, best = max(rivals, key=lambda item: item[0])
                    own_best = max((cosine(face.embedding, ref) for ref in own), default=0.0)
                    if best_score >= best.threshold and best_score > own_best:
                        result.elsewhere.append((face.source, best.name, round(best_score, 3)))
                        continue
                if check_outliers and len(batch) + len(own) >= 3:
                    peers = [e for i, e in enumerate(batch) if i != index] + own
                    likeness = max((cosine(face.embedding, peer) for peer in peers), default=1.0)
                    if likeness < OUTLIER_SIMILARITY:
                        result.outliers.append(face.source)
                        continue
                if len(stored.faces) >= self.max_faces:
                    oldest = next((r for r in stored.faces if r.kind == "confirmed"), None)
                    if kind != "confirmed" or oldest is None:
                        result.refused_full += 1
                        continue
                    stored.faces.remove(oldest)
                    (self._dir(pid) / oldest.file).unlink(missing_ok=True)
                    own = [decode(record.embedding) for record in stored.faces]
                    result.dropped_for_room += 1
                face_id = "f" + hashlib.sha256(face.crop).hexdigest()[:12]
                relative = f"{FACES_DIR}/{face_id}.jpg"
                write_private(self._dir(pid) / relative, face.crop)
                stored.faces.append(
                    FaceRecord(
                        id=face_id,
                        file=relative,
                        embedding=encode(face.embedding),
                        kind=kind,
                        source=face.source,
                        added_at=_now(),
                        face_px=face.face_px,
                    )
                )
                own.append(face.embedding)
                result.added.append(face_id)
            self._write_faces(pid, stored)
        return result

    def remove_face(self, pid: str, face_id: str) -> bool:
        with self._locked():
            stored = self._read_faces(pid)
            record = next((r for r in stored.faces if r.id == face_id), None)
            if record is None:
                return False
            stored.faces.remove(record)
            (self._dir(pid) / record.file).unlink(missing_ok=True)
            self._write_faces(pid, stored)
            return True

    def reject(self, pid: str, score: float, where: str | None) -> float:
        """Record that a match with this score was wrong; returns the new threshold."""
        with self._locked():
            person = self._read_person(pid)
            if person is None:
                raise StoreError(f"no person '{pid}'")
            person.rejected.append(Rejection(score=round(score, 3), at=_now(), where=where))
            self._write_person(person)
            return effective_threshold(person, self.threshold)
