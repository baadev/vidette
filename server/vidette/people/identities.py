"""Anonymous identities: faces nobody enrolled, recognized when they come back (ADR-0014).

With `people.track_unknown`, an unknown face that is big and clear enough is compared with
the identities seen before. A close match adds a sighting to that identity; a face like
nobody's starts a new one, shown as "stranger #12". Nobody is named: a household member
may give an identity a name later, mark it suspicious or ignored, merge two identities
that are one person, split one that is two, or promote it to a known person.

    <people.identities.dir>/          0700, never inside a git work tree
      identities.db                   0600, SQLite: identities, sightings, embeddings
      crops/<sighting>.jpg            0600: the best aligned face crop of each sighting

What is kept, and for how long:

- per sighting: when, which camera, which clip, how many faces and how big, the best
  face crop and up to three embeddings (the largest, mutually different faces);
- an identity nobody named or flagged, and everything it holds, is deleted
  `retention_days` (30) after it was last seen; named, suspicious, ignored and promoted
  identities stay until someone deletes them;
- deleting an identity removes its embeddings, crops and sightings; SQLite runs with
  `secure_delete` and the write-ahead log is truncated after a delete, so the bytes are
  overwritten, not just unlinked.

Matching is conservative on purpose: two strangers merged into one identity make a
recurrence that never happened, while one person split in two only undercounts and is
fixed with a merge. A sighting joins an identity at `match` (0.5) or above and only when
no other identity is within `margin`; a face that close to two identities joins neither.
Faces smaller than `min_face_px` never create or join an identity, and an unknown face
close to someone enrolled (`near_known`) is left alone: it is probably that person.

Appearance is never used: no age, gender, skin or clothing attribute exists anywhere in
this module, only the face embedding the engine computes, and the store's own counts.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import sys
import time
from array import array
from collections import Counter, deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import get_args

from vidette.paths import git_work_tree, private_dir, too_open, write_private
from vidette.people.report import IdentityBrief, IdentityStatus
from vidette.people.store import NewFace, PeopleStore
from vidette.people.vectors import Vector, cosine

logger = logging.getLogger(__name__)

DB_FILE = "identities.db"
CROPS_DIR = "crops"
STATUSES: tuple[str, ...] = get_args(IdentityStatus)
SETTABLE: frozenset[str] = frozenset({"unknown", "suspicious", "ignored"})  # "known": promote
RECENT_S = 7 * 86400.0  # what a brief lists, for recurrence rules
MAX_RECENT = 50
DIVERSE = 0.92  # two faces of one sighting this alike are one reference
EXPIRE_EVERY_S = 3600.0
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS identities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,   -- AUTOINCREMENT: numbers never reused
    name        TEXT,
    status      TEXT NOT NULL DEFAULT 'unknown',
    person      TEXT,                                -- the enrolled person it was promoted to
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sightings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    identity    INTEGER NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
    at          REAL NOT NULL,
    camera      TEXT NOT NULL,
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    clip        TEXT,
    faces       INTEGER NOT NULL,
    face_px     INTEGER NOT NULL,
    crop        TEXT,                                -- file under crops/, or NULL
    UNIQUE (identity, source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_sightings_identity ON sightings(identity, at);
CREATE INDEX IF NOT EXISTS idx_sightings_source ON sightings(source, source_id);
CREATE TABLE IF NOT EXISTS refs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    identity    INTEGER NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
    sighting    INTEGER NOT NULL REFERENCES sightings(id) ON DELETE CASCADE,
    embedding   BLOB NOT NULL,                       -- float32, little-endian
    face_px     INTEGER NOT NULL,
    best        INTEGER NOT NULL DEFAULT 0           -- the face the sighting's crop shows
);
CREATE INDEX IF NOT EXISTS idx_refs_identity ON refs(identity);
CREATE TABLE IF NOT EXISTS checked (                 -- clips the backfill has looked at
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    at          REAL NOT NULL,
    PRIMARY KEY (source, source_id)
);
"""


class IdentityError(Exception):
    """The identity store cannot do what was asked; the message says what to do."""


@dataclass(frozen=True)
class IdentitySettings:
    match: float = 0.5  # a sighting joins an identity at this similarity or above
    margin: float = 0.06  # ...and only when no other identity is this close to it
    min_face_px: int = 64  # the largest face of a sighting must be at least this tall
    near_known: float = 0.3  # an unknown face this close to someone enrolled is left alone
    retention_days: int = 30  # unnamed, unflagged identities go this long after last seen
    refs_per_sighting: int = 3
    max_refs: int = 40  # per identity, largest faces first, for matching


@dataclass(frozen=True)
class GroupFace:
    """One readable face of an unknown person in a clip (from the face check)."""

    embedding: Vector
    face_px: int
    score: float = 1.0
    crop: bytes | None = None  # aligned JPEG


@dataclass(frozen=True)
class SightingContext:
    """Where and when: the incident the faces come from."""

    at: float
    camera: str
    source: str
    source_id: str
    clip: str | None = None


@dataclass(frozen=True)
class IdentityRow:
    id: int
    name: str | None
    status: str
    person: str | None
    created_at: float
    sightings: int
    days: int
    first_seen: float | None
    last_seen: float | None
    cameras: list[str]
    cover: int | None  # the sighting whose crop shows the largest face


@dataclass(frozen=True)
class SightingRow:
    id: int
    identity: int
    at: float
    camera: str
    source: str
    source_id: str
    clip: str | None
    faces: int
    face_px: int
    has_crop: bool


@dataclass(frozen=True)
class Similar:
    id: int
    score: float


@dataclass
class Assignment:
    """What `assign` did, for logs and the backfill's summary."""

    brief: IdentityBrief | None
    reason: str  # "joined", "new", "too small", "near known", "ambiguous", "past retention"
    scores: list[tuple[int, float]] = field(default_factory=list)  # closest identities
    face_px: int = 0  # the sighting's largest face
    known_score: float = 0.0  # its best similarity to anyone enrolled


def _pack(vector: Vector) -> bytes:
    data = array("f", vector)
    if sys.byteorder != "little":  # pragma: no cover - big-endian only
        data.byteswap()
    return data.tobytes()


def _unpack(blob: bytes) -> Vector:
    data = array("f")
    data.frombytes(blob)
    if sys.byteorder != "little":  # pragma: no cover
        data.byteswap()
    return tuple(float(value) for value in data)


def clean_name(name: str | None) -> str | None:
    if name is None:
        return None
    name = " ".join(name.split())
    if not name:
        return None
    if len(name) > 40 or not name.isprintable():
        raise IdentityError("a name is 1 to 40 printable characters")
    return name


class IdentityStore:
    """One identities folder. Every call opens its own connection, so the server's worker
    thread, its API and a command-line backfill can all use it; SQLite serialises the
    writes."""

    def __init__(
        self,
        root: Path,
        settings: IdentitySettings | None = None,
        *,
        clock: Callable[[], float] = time.time,
        allow_inside_git: bool = False,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.settings = settings or IdentitySettings()
        self._clock = clock
        self._last_expire = 0.0
        self.counts: Counter[str] = Counter()  # what `assign` did, by reason
        self.recent: deque[Assignment] = deque(maxlen=50)  # the last few, for the CLI
        repo = git_work_tree(self.root)
        if repo is not None and not allow_inside_git:
            raise IdentityError(
                f"{self.root} is inside the git work tree {repo}; face embeddings and crops "
                "are biometric data and must never be committed. Choose a folder outside any "
                "repository (people.identities.dir, default ~/vidette-data/identities)"
            )

    # --- files -----------------------------------------------------------------------

    @property
    def db_path(self) -> Path:
        return self.root / DB_FILE

    @property
    def crops_dir(self) -> Path:
        return self.root / CROPS_DIR

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        private_dir(self.root)
        if not self.db_path.exists():
            fd = os.open(self.db_path, os.O_WRONLY | os.O_CREAT, 0o600)
            os.close(fd)
        conn = sqlite3.connect(self.db_path, timeout=15.0, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA secure_delete=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                conn.executescript(_SCHEMA)
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            for suffix in ("-wal", "-shm"):
                side = self.db_path.with_name(DB_FILE + suffix)
                if side.exists() and too_open(side):
                    side.chmod(0o600)
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def _crop_file(self, name: str | None) -> Path | None:
        if not name:
            return None
        path = (self.crops_dir / name).resolve()
        if not path.is_relative_to(self.crops_dir.resolve()) or not path.is_file():
            return None
        return path

    def _remove_crops(self, names: list[str]) -> None:
        for name in names:
            path = self._crop_file(name)
            if path is not None:
                path.unlink(missing_ok=True)

    def permission_warnings(self) -> list[str]:
        if not self.root.is_dir():
            return []
        loose = [p for p in [self.root, *self.root.rglob("*")] if too_open(p)]
        if not loose:
            return []
        return [
            f"{len(loose)} item(s) under {self.root} are readable by other users (e.g. "
            f"{loose[0]}); run: chmod -R go-rwx {self.root}"
        ]

    # --- matching ----------------------------------------------------------------------

    def _references(self, conn: sqlite3.Connection) -> dict[int, list[Vector]]:
        """Each identity that can still be matched (not promoted), its largest faces."""
        rows = conn.execute(
            "SELECT r.identity, r.embedding FROM refs r JOIN identities i ON i.id = r.identity"
            " WHERE i.status != 'known' ORDER BY r.identity, r.face_px DESC, r.id DESC"
        ).fetchall()
        refs: dict[int, list[Vector]] = {}
        for row in rows:
            kept = refs.setdefault(int(row["identity"]), [])
            if len(kept) < self.settings.max_refs:
                kept.append(_unpack(row["embedding"]))
        return refs

    @staticmethod
    def _score(probes: list[Vector], refs: list[Vector]) -> float:
        return max((cosine(p, r) for p in probes for r in refs), default=-1.0)

    def _probes(self, faces: list[GroupFace]) -> list[GroupFace]:
        """The largest readable faces of the sighting, skipping near-copies."""
        chosen: list[GroupFace] = []
        for face in sorted(faces, key=lambda f: (-f.face_px, -f.score)):
            if face.face_px < self.settings.min_face_px:
                continue
            if any(cosine(face.embedding, c.embedding) >= DIVERSE for c in chosen):
                continue
            chosen.append(face)
            if len(chosen) >= self.settings.refs_per_sighting:
                break
        return chosen

    def assign(
        self, faces: list[GroupFace], context: SightingContext, *, known_score: float = 0.0
    ) -> Assignment:
        """Add one sighting of an unknown person: to the identity it matches, or to a new
        one. `known_score`: the face's best similarity to anyone enrolled."""
        result = self._assign(faces, context, known_score)
        result.face_px = max((f.face_px for f in faces), default=0)
        result.known_score = round(known_score, 3)
        self.counts[result.reason] += 1
        self.recent.append(result)
        return result

    def _assign(
        self, faces: list[GroupFace], context: SightingContext, known_score: float
    ) -> Assignment:
        probes = self._probes(faces)
        if not probes:
            return Assignment(None, "too small")
        if known_score >= self.settings.near_known:
            return Assignment(None, "near known")
        if context.at < self._clock() - self.settings.retention_days * 86400.0:
            return Assignment(None, "past retention")  # a backfill of old clips
        self.expire_now_and_then()
        vectors = [p.embedding for p in probes]
        with self._write() as conn:
            references = self._references(conn)
            scored = sorted(
                ((identity, self._score(vectors, refs)) for identity, refs in references.items()),
                key=lambda item: -item[1],
            )
            top = [(identity, round(score, 3)) for identity, score in scored[:3]]
            new = False
            if scored and scored[0][1] >= self.settings.match:
                if len(scored) > 1 and scored[0][1] - scored[1][1] < self.settings.margin:
                    return Assignment(None, "ambiguous", top)
                identity = scored[0][0]
            else:
                now = self._clock()
                cursor = conn.execute(
                    "INSERT INTO identities (status, created_at, updated_at)"
                    " VALUES ('unknown', ?, ?)",
                    (now, now),
                )
                assert cursor.lastrowid is not None
                identity, new = int(cursor.lastrowid), True
            self._add_sighting(conn, identity, probes, faces, context)
            brief = self._brief(conn, identity, context.at, new=new)
        return Assignment(brief, "new" if new else "joined", top)

    def _add_sighting(
        self,
        conn: sqlite3.Connection,
        identity: int,
        probes: list[GroupFace],
        faces: list[GroupFace],
        context: SightingContext,
    ) -> None:
        best = probes[0]
        row = conn.execute(
            "SELECT id, crop, face_px FROM sightings"
            " WHERE identity = ? AND source = ? AND source_id = ?",
            (identity, context.source, context.source_id),
        ).fetchone()
        if row is not None and int(row["face_px"]) > best.face_px:
            # the same clip again with a smaller face (a second group of one person, or a
            # poorer check): the sighting keeps what it has
            conn.execute(
                "UPDATE sightings SET faces = MAX(faces, ?) WHERE id = ?", (len(faces), row["id"])
            )
            return
        if row is not None:  # the same clip checked again: replace what it gave
            sighting = int(row["id"])
            conn.execute("DELETE FROM refs WHERE sighting = ?", (sighting,))
            conn.execute(
                "UPDATE sightings SET at = ?, camera = ?, clip = ?, faces = ?, face_px = ?"
                " WHERE id = ?",
                (context.at, context.camera, context.clip, len(faces), best.face_px, sighting),
            )
            old_crop = row["crop"]
        else:
            cursor = conn.execute(
                "INSERT INTO sightings (identity, at, camera, source, source_id, clip, faces,"
                " face_px) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    identity,
                    context.at,
                    context.camera,
                    context.source,
                    context.source_id,
                    context.clip,
                    len(faces),
                    best.face_px,
                ),
            )
            assert cursor.lastrowid is not None
            sighting = int(cursor.lastrowid)
            old_crop = None
        for index, probe in enumerate(probes):
            conn.execute(
                "INSERT INTO refs (identity, sighting, embedding, face_px, best)"
                " VALUES (?, ?, ?, ?, ?)",
                (identity, sighting, _pack(probe.embedding), probe.face_px, int(index == 0)),
            )
        crop = None
        if best.crop is not None:
            crop = f"{sighting}.jpg"
            write_private(self.crops_dir / crop, best.crop)
        elif old_crop:
            self._remove_crops([old_crop])
        conn.execute("UPDATE sightings SET crop = ? WHERE id = ?", (crop, sighting))
        conn.execute("UPDATE identities SET updated_at = ? WHERE id = ?", (self._clock(), identity))

    def _brief(
        self, conn: sqlite3.Connection, identity: int, at: float, *, new: bool = False
    ) -> IdentityBrief:
        row = conn.execute("SELECT * FROM identities WHERE id = ?", (identity,)).fetchone()
        if row is None:
            raise IdentityError(f"there is no identity #{identity}")
        counts = conn.execute(
            "SELECT COUNT(*) AS n, MIN(at) AS first,"
            " COUNT(DISTINCT date(at, 'unixepoch', 'localtime')) AS days"
            " FROM sightings WHERE identity = ? AND at <= ?",
            (identity, at),
        ).fetchone()
        recent = conn.execute(
            "SELECT at, camera FROM sightings WHERE identity = ? AND at <= ? AND at >= ?"
            " ORDER BY at DESC LIMIT ?",
            (identity, at, at - RECENT_S, MAX_RECENT),
        ).fetchall()
        return IdentityBrief(
            id=identity,
            name=row["name"],
            status=row["status"],
            new=new,
            at=at,
            sightings=max(1, int(counts["n"])),
            days=max(1, int(counts["days"])),
            first_seen=float(counts["first"] if counts["first"] is not None else at),
            recent=[(float(r["at"]), str(r["camera"])) for r in recent],
        )

    def brief(self, identity: int, at: float | None = None) -> IdentityBrief:
        with self._connect() as conn:
            return self._brief(conn, identity, self._clock() if at is None else at)

    # --- reading -------------------------------------------------------------------------

    def _row(self, conn: sqlite3.Connection, row: sqlite3.Row) -> IdentityRow:
        identity = int(row["id"])
        counts = conn.execute(
            "SELECT COUNT(*) AS n, MIN(at) AS first, MAX(at) AS last,"
            " COUNT(DISTINCT date(at, 'unixepoch', 'localtime')) AS days"
            " FROM sightings WHERE identity = ?",
            (identity,),
        ).fetchone()
        cameras = [
            str(r["camera"])
            for r in conn.execute(
                "SELECT camera, COUNT(*) AS n FROM sightings WHERE identity = ?"
                " GROUP BY camera ORDER BY n DESC, camera",
                (identity,),
            )
        ]
        cover = conn.execute(
            "SELECT id FROM sightings WHERE identity = ? AND crop IS NOT NULL"
            " ORDER BY face_px DESC, at DESC LIMIT 1",
            (identity,),
        ).fetchone()
        return IdentityRow(
            id=identity,
            name=row["name"],
            status=str(row["status"]),
            person=row["person"],
            created_at=float(row["created_at"]),
            sightings=int(counts["n"]),
            days=int(counts["days"]),
            first_seen=counts["first"],
            last_seen=counts["last"],
            cameras=cameras,
            cover=None if cover is None else int(cover["id"]),
        )

    def entries(self, *, status: str | None = None) -> list[IdentityRow]:
        """Every identity, most recently seen first (retention applied first, hourly)."""
        if not self.db_path.exists():
            return []
        self.expire_now_and_then()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT i.* FROM identities i LEFT JOIN sightings s ON s.identity = i.id"
                " GROUP BY i.id ORDER BY MAX(s.at) DESC NULLS LAST, i.id DESC"
            ).fetchall()
            out = [self._row(conn, row) for row in rows]
        return [row for row in out if status is None or row.status == status]

    def get(self, identity: int) -> IdentityRow | None:
        if not self.db_path.exists():
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM identities WHERE id = ?", (identity,)).fetchone()
            return None if row is None else self._row(conn, row)

    def require(self, identity: int) -> IdentityRow:
        row = self.get(identity)
        if row is None:
            raise IdentityError(f"there is no identity #{identity} (deleted or merged?)")
        return row

    def sightings(self, identity: int) -> list[SightingRow]:
        """Newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sightings WHERE identity = ? ORDER BY at DESC, id DESC",
                (identity,),
            ).fetchall()
        return [
            SightingRow(
                id=int(r["id"]),
                identity=int(r["identity"]),
                at=float(r["at"]),
                camera=str(r["camera"]),
                source=str(r["source"]),
                source_id=str(r["source_id"]),
                clip=r["clip"],
                faces=int(r["faces"]),
                face_px=int(r["face_px"]),
                has_crop=r["crop"] is not None,
            )
            for r in rows
        ]

    def sighting_crop(self, sighting: int) -> Path | None:
        with self._connect() as conn:
            row = conn.execute("SELECT crop FROM sightings WHERE id = ?", (sighting,)).fetchone()
        return None if row is None else self._crop_file(row["crop"])

    def sighting(self, sighting: int) -> SightingRow | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT identity FROM sightings WHERE id = ?", (sighting,)
            ).fetchone()
        if row is None:
            return None
        return next((s for s in self.sightings(int(row["identity"])) if s.id == sighting), None)

    def similar(self, identity: int, *, floor: float = 0.3, limit: int = 5) -> list[Similar]:
        """Other identities whose faces are this close: candidates for a merge."""
        with self._connect() as conn:
            refs = self._references(conn)
            own = [
                _unpack(r["embedding"])
                for r in conn.execute(
                    "SELECT embedding FROM refs WHERE identity = ? ORDER BY face_px DESC LIMIT ?",
                    (identity, self.settings.max_refs),
                )
            ]
        if not own:
            return []
        scored = [
            Similar(other, round(self._score(own, vectors), 3))
            for other, vectors in refs.items()
            if other != identity
        ]
        return sorted((s for s in scored if s.score >= floor), key=lambda s: -s.score)[:limit]

    def was_checked(self, source: str, source_id: str) -> bool:
        if not self.db_path.exists():
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM checked WHERE source = ? AND source_id = ?", (source, source_id)
            ).fetchone()
        return row is not None

    def mark_checked(self, source: str, source_id: str) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO checked (source, source_id, at) VALUES (?, ?, ?)",
                (source, source_id, self._clock()),
            )

    def stats(self) -> dict[str, int]:
        if not self.db_path.exists():
            return {"identities": 0, "sightings": 0, "references": 0}
        with self._connect() as conn:
            return {
                "identities": int(conn.execute("SELECT COUNT(*) FROM identities").fetchone()[0]),
                "sightings": int(conn.execute("SELECT COUNT(*) FROM sightings").fetchone()[0]),
                "references": int(conn.execute("SELECT COUNT(*) FROM refs").fetchone()[0]),
            }

    # --- changes -------------------------------------------------------------------------

    def rename(self, identity: int, name: str | None) -> IdentityRow:
        cleaned = clean_name(name)
        with self._write() as conn:
            self._exists(conn, identity)
            conn.execute(
                "UPDATE identities SET name = ?, updated_at = ? WHERE id = ?",
                (cleaned, self._clock(), identity),
            )
        return self.require(identity)

    def set_status(self, identity: int, status: str) -> IdentityRow:
        if status not in SETTABLE:
            raise IdentityError(
                f"status is one of {', '.join(sorted(SETTABLE))}; 'known' comes from promoting "
                "the identity to an enrolled person"
            )
        with self._write() as conn:
            self._exists(conn, identity)
            conn.execute(
                "UPDATE identities SET status = ?, person = NULL, updated_at = ? WHERE id = ?",
                (status, self._clock(), identity),
            )
        return self.require(identity)

    @staticmethod
    def _exists(conn: sqlite3.Connection, identity: int) -> sqlite3.Row:
        row: sqlite3.Row | None = conn.execute(
            "SELECT * FROM identities WHERE id = ?", (identity,)
        ).fetchone()
        if row is None:
            raise IdentityError(f"there is no identity #{identity} (deleted or merged?)")
        return row

    def merge(self, source: int, into: int) -> IdentityRow:
        """Move every sighting of `source` to `into` and delete `source`. A name or a
        status `into` lacks is taken from `source`; the same clip in both is kept once."""
        if source == into:
            raise IdentityError("an identity cannot be merged into itself")
        with self._write() as conn:
            moving = self._exists(conn, source)
            target = self._exists(conn, into)
            duplicates = conn.execute(
                "SELECT s.id, s.crop FROM sightings s JOIN sightings t ON t.identity = ?"
                " AND t.source = s.source AND t.source_id = s.source_id WHERE s.identity = ?",
                (into, source),
            ).fetchall()
            for row in duplicates:
                conn.execute("DELETE FROM sightings WHERE id = ?", (row["id"],))
            conn.execute("UPDATE sightings SET identity = ? WHERE identity = ?", (into, source))
            conn.execute("UPDATE refs SET identity = ? WHERE identity = ?", (into, source))
            name = target["name"] or moving["name"]
            status = target["status"]
            if status == "unknown" and moving["status"] in ("suspicious", "ignored"):
                status = moving["status"]
            conn.execute(
                "UPDATE identities SET name = ?, status = ?, updated_at = ? WHERE id = ?",
                (name, status, self._clock(), into),
            )
            conn.execute("DELETE FROM identities WHERE id = ?", (source,))
        self._remove_crops([str(r["crop"]) for r in duplicates if r["crop"]])
        self._checkpoint()
        return self.require(into)

    def split(self, identity: int, sightings: list[int]) -> IdentityRow:
        """Move these sightings (with their faces) to a new identity."""
        wanted = sorted(set(sightings))
        if not wanted:
            raise IdentityError("choose the sightings that are someone else")
        with self._write() as conn:
            self._exists(conn, identity)
            marks = ",".join("?" * len(wanted))
            found = [
                int(r["id"])
                for r in conn.execute(
                    f"SELECT id FROM sightings WHERE identity = ? AND id IN ({marks})",
                    (identity, *wanted),
                )
            ]
            if len(found) != len(wanted):
                raise IdentityError(f"identity #{identity} does not have all those sightings")
            total = conn.execute(
                "SELECT COUNT(*) FROM sightings WHERE identity = ?", (identity,)
            ).fetchone()[0]
            if len(found) == total:
                raise IdentityError("that is every sighting; nothing would be left to split from")
            now = self._clock()
            cursor = conn.execute(
                "INSERT INTO identities (status, created_at, updated_at) VALUES ('unknown', ?, ?)",
                (now, now),
            )
            assert cursor.lastrowid is not None
            new = int(cursor.lastrowid)
            conn.execute(f"UPDATE sightings SET identity = ? WHERE id IN ({marks})", (new, *found))
            conn.execute(f"UPDATE refs SET identity = ? WHERE sighting IN ({marks})", (new, *found))
        return self.require(new)

    def delete(self, identity: int) -> int:
        """Delete an identity with every embedding, crop and sighting it holds; returns
        how many sightings went."""
        with self._write() as conn:
            self._exists(conn, identity)
            crops = [
                str(r["crop"])
                for r in conn.execute(
                    "SELECT crop FROM sightings WHERE identity = ? AND crop IS NOT NULL",
                    (identity,),
                )
            ]
            count = conn.execute(
                "SELECT COUNT(*) FROM sightings WHERE identity = ?", (identity,)
            ).fetchone()[0]
            conn.execute("DELETE FROM identities WHERE id = ?", (identity,))
        self._remove_crops(crops)
        self._checkpoint()
        return int(count)

    def _checkpoint(self) -> None:
        """Fold the write-ahead log into the database and truncate it, so a deleted
        embedding is not left behind in the log file."""
        with self._connect() as conn, contextlib.suppress(sqlite3.OperationalError):
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def expire(self, now: float | None = None) -> int:
        """Delete identities nobody named or flagged that were last seen more than
        `retention_days` ago (and identities left without a sighting). Returns how many."""
        if not self.db_path.exists():
            return 0
        now = self._clock() if now is None else now
        cutoff = now - self.settings.retention_days * 86400.0
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT i.id FROM identities i LEFT JOIN sightings s ON s.identity = i.id"
                " WHERE i.status = 'unknown' AND i.name IS NULL"
                " GROUP BY i.id HAVING MAX(s.at) IS NULL OR MAX(s.at) < ?",
                (cutoff,),
            ).fetchall()
        gone = 0
        for row in rows:
            with contextlib.suppress(IdentityError):
                self.delete(int(row["id"]))
                gone += 1
        self._sweep_crops()
        return gone

    def expire_now_and_then(self) -> None:
        now = self._clock()
        if now - self._last_expire < EXPIRE_EVERY_S:
            return
        self._last_expire = now
        try:
            gone = self.expire(now)
        except (sqlite3.Error, OSError) as exc:
            logger.warning("identity retention could not run: %s", exc)
            return
        if gone:
            logger.info("identity retention: %d identities expired", gone)

    def _sweep_crops(self) -> None:
        """Crop files no sighting points to (a write that failed half way)."""
        if not self.crops_dir.is_dir():
            return
        with self._connect() as conn:
            rows = conn.execute("SELECT crop FROM sightings WHERE crop IS NOT NULL").fetchall()
        used = {str(row[0]) for row in rows}
        for path in self.crops_dir.glob("*.jpg"):
            if path.name not in used and path.stat().st_mtime < self._clock() - 3600:
                path.unlink(missing_ok=True)

    # --- promotion -----------------------------------------------------------------------

    def faces_for_promotion(self, identity: int) -> list[tuple[Vector, bytes, int, int]]:
        """(embedding, crop, face px, sighting) of each sighting's best face, largest
        first: what becomes an enrolled person's references."""
        with self._connect() as conn:
            self._exists(conn, identity)
            rows = conn.execute(
                "SELECT r.embedding, r.face_px, s.id AS sighting, s.crop FROM refs r"
                " JOIN sightings s ON s.id = r.sighting"
                " WHERE r.identity = ? AND r.best = 1 AND s.crop IS NOT NULL"
                " ORDER BY r.face_px DESC",
                (identity,),
            ).fetchall()
        out = []
        for row in rows:
            path = self._crop_file(row["crop"])
            if path is not None:
                embedding = _unpack(row["embedding"])
                out.append(
                    (embedding, path.read_bytes(), int(row["face_px"]), int(row["sighting"]))
                )
        return out

    def mark_known(self, identity: int, person: str, name: str) -> IdentityRow:
        with self._write() as conn:
            row = self._exists(conn, identity)
            conn.execute(
                "UPDATE identities SET status = 'known', person = ?, name = ?, updated_at = ?"
                " WHERE id = ?",
                (person, row["name"] or name, self._clock(), identity),
            )
        return self.require(identity)


def promote(
    identities: IdentityStore,
    identity: int,
    people: PeopleStore,
    person: str,
    *,
    limit: int = 20,
) -> tuple[IdentityRow, int]:
    """Make an identity a known person: its sightings' best faces become `person`'s
    references in the people store (largest first, at most `limit`), and the identity is
    marked known, so it is no longer matched as a stranger. Returns (row, faces added)."""
    target = people.require(person)
    faces = [
        NewFace(embedding, crop, px, f"identity:{identity}#sighting-{sighting}")
        for embedding, crop, px, sighting in identities.faces_for_promotion(identity)[:limit]
    ]
    added = 0
    if faces:
        result = people.add_faces(target.id, faces, kind="confirmed", check_outliers=False)
        added = len(result.added)
    return identities.mark_known(identity, target.id, target.name), added
