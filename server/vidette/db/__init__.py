"""SQLite store (WAL) — the single database of ADR-0008.

CONTRACT FILE: signatures and row shapes below are the interface the rest of M1 codes
against (auth, recorder, janitor, API routers). Implementation notes:

- aiosqlite; WAL mode; foreign keys on; one `Database` instance per process (single-writer
  discipline per ADR-0008 — all writes go through this class).
- Schema DDL + migrations live in `vidette/db/schema.py`; `connect()` migrates using the
  `meta(key='schema_version')` row. Migrations are append-only.
- Timestamps are unix epoch seconds (float, UTC). No datetime objects cross this boundary.
- No ORM. Plain SQL, parameterized always.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from vidette.db.schema import MIGRATIONS, SCHEMA_VERSION

# SQLite's default host-parameter limit is 999 on older builds; stay well under it when
# expanding IN (...) lists.
_MAX_PARAMS_PER_STATEMENT = 500


@dataclass(frozen=True)
class UserRow:
    id: int
    username: str
    password_hash: str
    role: str  # "admin" | "viewer"
    created_at: float


@dataclass(frozen=True)
class SessionRow:
    token_hash: str
    user_id: int
    created_at: float
    expires_at: float


@dataclass(frozen=True)
class ApiTokenRow:
    id: int
    name: str
    token_hash: str
    scopes: str  # comma-separated, e.g. "read:events,read:streams"
    user_id: int
    created_at: float
    last_used_at: float | None
    revoked_at: float | None


@dataclass(frozen=True)
class SegmentRow:
    id: int
    camera: str
    start_ts: float
    end_ts: float
    path: str  # absolute path on the media volume
    size_bytes: int
    klass: str  # continuous | motion | event | favorite (SegmentClass values)
    codec: str | None


@dataclass(frozen=True)
class HourBucket:
    hour_start_ts: float
    recorded_seconds: float
    bytes: int


@dataclass(frozen=True)
class SystemEventRow:
    id: int
    at: float
    kind: str  # e.g. "storage.pressure", "storage.write_failed", "recorder.stalled"
    payload: dict[str, Any]


def _user(row: sqlite3.Row) -> UserRow:
    return UserRow(
        id=row["id"],
        username=row["username"],
        password_hash=row["password_hash"],
        role=row["role"],
        created_at=row["created_at"],
    )


def _session(row: sqlite3.Row) -> SessionRow:
    return SessionRow(
        token_hash=row["token_hash"],
        user_id=row["user_id"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


def _api_token(row: sqlite3.Row) -> ApiTokenRow:
    return ApiTokenRow(
        id=row["id"],
        name=row["name"],
        token_hash=row["token_hash"],
        scopes=row["scopes"],
        user_id=row["user_id"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        revoked_at=row["revoked_at"],
    )


def _segment(row: sqlite3.Row) -> SegmentRow:
    return SegmentRow(
        id=row["id"],
        camera=row["camera"],
        start_ts=row["start_ts"],
        end_ts=row["end_ts"],
        path=row["path"],
        size_bytes=row["size_bytes"],
        klass=row["klass"],
        codec=row["codec"],
    )


def _system_event(row: sqlite3.Row) -> SystemEventRow:
    return SystemEventRow(
        id=row["id"],
        at=row["at"],
        kind=row["kind"],
        payload=json.loads(row["payload"]),
    )


class Database:
    """All methods are safe to call from any task; writes serialize internally."""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self._clock = clock
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError(
                "Database is not connected — call `await db.connect()` before using it."
            )
        return self._conn

    async def connect(self) -> None:
        """Open, set WAL/pragmas, run migrations."""
        if self._conn is not None:
            return  # already connected; connect() is idempotent
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self.path)
        try:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute("PRAGMA busy_timeout=5000")
            await self._migrate(conn)
        except BaseException:
            await conn.close()
            raise
        self._conn = conn

    async def _migrate(self, conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        async with conn.execute("SELECT value FROM meta WHERE key = 'schema_version'") as cur:
            row = await cur.fetchone()
        current = int(row["value"]) if row is not None else 0
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"database {self.path} has schema version {current}, but this build only "
                f"knows version {SCHEMA_VERSION} — upgrade Vidette or restore the database "
                "file that matches this version."
            )
        for step, ddl in enumerate(MIGRATIONS[current:], start=current + 1):
            await conn.executescript(ddl)
            await conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(step),),
            )
            await conn.commit()

    async def close(self) -> None:
        if self._conn is None:
            return
        conn, self._conn = self._conn, None
        await conn.close()

    # --- users --------------------------------------------------------------------------
    async def count_users(self) -> int:
        async with self._db.execute("SELECT COUNT(*) AS n FROM users") as cur:
            row = await cur.fetchone()
        assert row is not None  # COUNT(*) always yields one row
        return int(row["n"])

    async def create_user(self, username: str, password_hash: str, role: str = "admin") -> int:
        """Returns user id. Raises ValueError on duplicate username."""
        async with self._write_lock:
            try:
                async with self._db.execute(
                    "INSERT INTO users (username, password_hash, role, created_at) "
                    "VALUES (?, ?, ?, ?) RETURNING id",
                    (username, password_hash, role, self._clock()),
                ) as cur:
                    row = await cur.fetchone()
            except sqlite3.IntegrityError as exc:
                raise ValueError("username already exists") from exc
            await self._db.commit()
        assert row is not None
        return int(row["id"])

    async def get_user_by_username(self, username: str) -> UserRow | None:
        async with self._db.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ) as cur:
            row = await cur.fetchone()
        return _user(row) if row is not None else None

    async def get_user(self, user_id: int) -> UserRow | None:
        async with self._db.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        return _user(row) if row is not None else None

    # --- sessions -----------------------------------------------------------------------
    async def create_session(self, token_hash: str, user_id: int, expires_at: float) -> None:
        async with self._write_lock:
            await self._db.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (token_hash, user_id, self._clock(), expires_at),
            )
            await self._db.commit()

    async def get_session(self, token_hash: str) -> SessionRow | None:
        """Returns the row even if expired; expiry policy lives in the auth service."""
        async with self._db.execute(
            "SELECT * FROM sessions WHERE token_hash = ?", (token_hash,)
        ) as cur:
            row = await cur.fetchone()
        return _session(row) if row is not None else None

    async def delete_session(self, token_hash: str) -> None:
        async with self._write_lock:
            await self._db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            await self._db.commit()

    async def purge_expired_sessions(self, now: float) -> int:
        async with self._write_lock:
            async with self._db.execute(
                "DELETE FROM sessions WHERE expires_at <= ?", (now,)
            ) as cur:
                purged = cur.rowcount
            await self._db.commit()
        return purged

    # --- api tokens ---------------------------------------------------------------------
    async def create_api_token(
        self, name: str, token_hash: str, scopes: str, user_id: int
    ) -> int:
        async with self._write_lock:
            async with self._db.execute(
                "INSERT INTO api_tokens (name, token_hash, scopes, user_id, created_at) "
                "VALUES (?, ?, ?, ?, ?) RETURNING id",
                (name, token_hash, scopes, user_id, self._clock()),
            ) as cur:
                row = await cur.fetchone()
            await self._db.commit()
        assert row is not None
        return int(row["id"])

    async def get_api_token_by_hash(self, token_hash: str) -> ApiTokenRow | None:
        async with self._db.execute(
            "SELECT * FROM api_tokens WHERE token_hash = ?", (token_hash,)
        ) as cur:
            row = await cur.fetchone()
        return _api_token(row) if row is not None else None

    async def list_api_tokens(self) -> list[ApiTokenRow]:
        async with self._db.execute("SELECT * FROM api_tokens ORDER BY id") as cur:
            rows = await cur.fetchall()
        return [_api_token(row) for row in rows]

    async def revoke_api_token(self, token_id: int, now: float) -> bool:
        async with self._write_lock:
            async with self._db.execute(
                "UPDATE api_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (now, token_id),
            ) as cur:
                revoked = cur.rowcount > 0
            await self._db.commit()
        return revoked

    async def touch_api_token(self, token_id: int, now: float) -> None:
        async with self._write_lock:
            await self._db.execute(
                "UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (now, token_id)
            )
            await self._db.commit()

    # --- segments -----------------------------------------------------------------------
    async def add_segment(
        self,
        camera: str,
        start_ts: float,
        end_ts: float,
        path: str,
        size_bytes: int,
        klass: str = "continuous",
        codec: str | None = None,
    ) -> int:
        """Upsert by path (recorder may re-announce after restart)."""
        async with self._write_lock:
            async with self._db.execute(
                "INSERT INTO segments (camera, start_ts, end_ts, path, size_bytes, klass, codec)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(path) DO UPDATE SET"
                "   camera = excluded.camera,"
                "   start_ts = excluded.start_ts,"
                "   end_ts = excluded.end_ts,"
                "   size_bytes = excluded.size_bytes,"
                "   klass = excluded.klass,"
                "   codec = excluded.codec"
                " RETURNING id",
                (camera, start_ts, end_ts, path, size_bytes, klass, codec),
            ) as cur:
                row = await cur.fetchone()
            await self._db.commit()
        assert row is not None
        return int(row["id"])

    async def segments_between(
        self, camera: str, start_ts: float, end_ts: float
    ) -> list[SegmentRow]:
        """Segments overlapping [start_ts, end_ts), ordered by start_ts."""
        async with self._db.execute(
            "SELECT * FROM segments"
            " WHERE camera = ? AND end_ts > ? AND start_ts < ?"
            " ORDER BY start_ts",
            (camera, start_ts, end_ts),
        ) as cur:
            rows = await cur.fetchall()
        return [_segment(row) for row in rows]

    async def all_segments(self) -> list[SegmentRow]:
        async with self._db.execute(
            "SELECT * FROM segments ORDER BY camera, start_ts"
        ) as cur:
            rows = await cur.fetchall()
        return [_segment(row) for row in rows]

    async def latest_segment(self, camera: str) -> SegmentRow | None:
        async with self._db.execute(
            "SELECT * FROM segments WHERE camera = ? ORDER BY start_ts DESC LIMIT 1",
            (camera,),
        ) as cur:
            row = await cur.fetchone()
        return _segment(row) if row is not None else None

    async def get_segment(self, segment_id: int) -> SegmentRow | None:
        async with self._db.execute(
            "SELECT * FROM segments WHERE id = ?", (segment_id,)
        ) as cur:
            row = await cur.fetchone()
        return _segment(row) if row is not None else None

    async def delete_segments_by_path(self, paths: Sequence[str]) -> int:
        if not paths:
            return 0
        deleted = 0
        async with self._write_lock:
            for i in range(0, len(paths), _MAX_PARAMS_PER_STATEMENT):
                chunk = paths[i : i + _MAX_PARAMS_PER_STATEMENT]
                placeholders = ", ".join("?" for _ in chunk)
                async with self._db.execute(
                    f"DELETE FROM segments WHERE path IN ({placeholders})",
                    tuple(chunk),
                ) as cur:
                    deleted += cur.rowcount
            await self._db.commit()
        return deleted

    async def media_bytes_total(self) -> int:
        async with self._db.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) AS total FROM segments"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None  # aggregate always yields one row
        return int(row["total"])

    async def hourly_summary(
        self, camera: str, day_start_ts: float, day_end_ts: float
    ) -> list[HourBucket]:
        """Per-hour recorded seconds + bytes for the timeline; buckets with data only."""
        async with self._db.execute(
            "SELECT CAST(start_ts / 3600 AS INTEGER) * 3600 AS hour_start,"
            "       SUM(end_ts - start_ts) AS recorded_seconds,"
            "       SUM(size_bytes) AS total_bytes"
            " FROM segments"
            " WHERE camera = ? AND start_ts >= ? AND start_ts < ?"
            " GROUP BY hour_start"
            " ORDER BY hour_start",
            (camera, day_start_ts, day_end_ts),
        ) as cur:
            rows = await cur.fetchall()
        return [
            HourBucket(
                hour_start_ts=float(row["hour_start"]),
                recorded_seconds=float(row["recorded_seconds"]),
                bytes=int(row["total_bytes"]),
            )
            for row in rows
        ]

    # --- system events --------------------------------------------------------------------
    async def add_system_event(self, kind: str, payload: dict[str, Any]) -> int:
        async with self._write_lock:
            async with self._db.execute(
                "INSERT INTO system_events (at, kind, payload) VALUES (?, ?, ?) RETURNING id",
                (self._clock(), kind, json.dumps(payload)),
            ) as cur:
                row = await cur.fetchone()
            await self._db.commit()
        assert row is not None
        return int(row["id"])

    async def recent_system_events(
        self, limit: int = 100, since: float | None = None
    ) -> list[SystemEventRow]:
        """Newest first. `since` keeps only events strictly newer than that timestamp."""
        if since is None:
            sql = "SELECT * FROM system_events ORDER BY at DESC, id DESC LIMIT ?"
            params: tuple[float | int, ...] = (limit,)
        else:
            sql = (
                "SELECT * FROM system_events WHERE at > ? ORDER BY at DESC, id DESC LIMIT ?"
            )
            params = (since, limit)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_system_event(row) for row in rows]
