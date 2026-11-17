"""SQLite store (WAL) — the single database of ADR-0008.

CONTRACT FILE: signatures and row shapes below are the interface the rest of the server
codes against (auth, events intake, notifiers, API routers). Implementation notes:

- aiosqlite; WAL mode; foreign keys on; one `Database` instance per process (single-writer
  discipline per ADR-0008 — all writes go through this class).
- Schema DDL + migrations live in `vidette/db/schema.py`; `connect()` migrates using the
  `meta(key='schema_version')` row. Migrations are append-only.
- Timestamps are unix epoch seconds (float, UTC). No datetime objects cross this boundary.
- No ORM. Plain SQL, parameterized always.
- The v0.1 NVR tables (`segments`, `managed_cameras`) still exist because migrations are
  append-only; nothing on the v0.2 line reads or writes them (ADR-0009).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from vidette.db.schema import MIGRATIONS, SCHEMA_VERSION

logger = logging.getLogger(__name__)


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
class SystemEventRow:
    id: int
    at: float
    kind: str  # e.g. "storage.pressure", "storage.write_failed", "recorder.stalled"
    payload: dict[str, Any]


@dataclass(frozen=True)
class EventRow:
    """A row of `events` — the understood-event store (docs/events-and-automations.md).

    JSON columns arrive decoded: `kinds`/`zones` as lists, `geometry`/`intent` as dicts.
    """

    id: str  # opaque, lexicographically time-sortable (core.events.new_event_id)
    camera: str
    started_at: float
    ended_at: float | None
    state: str  # EventState values: observed | analyzing | confirmed | dismissed
    kinds: list[str]
    zones: list[str]
    geometry: dict[str, Any]
    summary: str | None
    intent: dict[str, Any] | None
    policy: str | None
    feedback: str | None  # "up" | "down" | None
    snapshot_path: str | None
    clip_path: str | None
    favorite: bool = False
    source: str | None = None  # upstream NVR ("frigate"); None for v0.1 rows
    source_ref: str | None = None  # upstream id (Frigate review id)
    upstream: dict[str, Any] | None = None  # what the upstream said (severity, summary)
    deep: dict[str, Any] | None = None  # the deep analysis (ADR-0019), next to `intent`


@dataclass(frozen=True)
class DeepJobRow:
    """One event in the deep-analysis queue (ADR-0019); `stats` decoded from JSON."""

    event_id: str
    camera: str
    started_at: float
    priority: int
    reason: str
    state: str  # queued | running | done | failed | skipped
    attempts: int
    queued_at: float
    taken_at: float | None
    done_at: float | None
    day: str | None
    error: str | None
    stats: dict[str, Any]


def _deep_job(row: sqlite3.Row) -> DeepJobRow:
    return DeepJobRow(
        event_id=str(row["event_id"]),
        camera=str(row["camera"]),
        started_at=float(row["started_at"]),
        priority=int(row["priority"]),
        reason=str(row["reason"]),
        state=str(row["state"]),
        attempts=int(row["attempts"]),
        queued_at=float(row["queued_at"]),
        taken_at=row["taken_at"],
        done_at=row["done_at"],
        day=row["day"],
        error=row["error"],
        stats=json.loads(row["stats"] or "{}"),
    )


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


def _system_event(row: sqlite3.Row) -> SystemEventRow:
    return SystemEventRow(
        id=row["id"],
        at=row["at"],
        kind=row["kind"],
        payload=json.loads(row["payload"]),
    )


def _event(row: sqlite3.Row) -> EventRow:
    return EventRow(
        id=row["id"],
        camera=row["camera"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        state=row["state"],
        kinds=json.loads(row["kinds"]),
        zones=json.loads(row["zones"]),
        geometry=json.loads(row["geometry"]),
        summary=row["summary"],
        intent=json.loads(row["intent"]) if row["intent"] is not None else None,
        policy=row["policy"],
        feedback=row["feedback"],
        favorite=bool(row["favorite"]),
        source=row["source"],
        source_ref=row["source_ref"],
        upstream=json.loads(row["upstream"]) if row["upstream"] is not None else None,
        snapshot_path=row["snapshot_path"],
        clip_path=row["clip_path"],
        deep=json.loads(row["deep"]) if "deep" in row.keys() and row["deep"] else None,  # noqa: SIM118
    )


@dataclass(frozen=True)
class PushSubscriptionRow:
    endpoint: str
    subscription: dict[str, Any]  # the browser's PushSubscription JSON
    user_id: int
    created_at: float


@dataclass(frozen=True)
class TelegramMessageRow:
    """A message the Telegram bot sent (ADR-0013); `data` decoded from JSON."""

    id: int
    chat_id: int
    thread_id: int | None
    message_id: int | None
    event_id: str | None
    kind: str  # alert | collapsed | digest | test | upgrade
    camera: str | None
    level: str | None
    created_at: float
    data: dict[str, Any]


def _telegram_message(row: sqlite3.Row) -> TelegramMessageRow:
    return TelegramMessageRow(
        id=int(row["id"]),
        chat_id=int(row["chat_id"]),
        thread_id=row["thread_id"],
        message_id=row["message_id"],
        event_id=row["event_id"],
        kind=str(row["kind"]),
        camera=row["camera"],
        level=row["level"],
        created_at=float(row["created_at"]),
        data=json.loads(row["data"] or "{}"),
    )


def _push_subscription(row: sqlite3.Row) -> PushSubscriptionRow:
    return PushSubscriptionRow(
        endpoint=row["endpoint"],
        subscription=json.loads(row["subscription"]),
        user_id=row["user_id"],
        created_at=row["created_at"],
    )


@dataclass(frozen=True)
class CalibrationRow:
    """One step of calibration (ADR-0020): a scenario on a camera made quieter or louder
    by marks, or taken back. `evidence` decoded from JSON."""

    id: int
    at: float
    policy: str
    scenario: str
    camera: str
    step_from: int
    step_to: int
    bar: float | None
    evidence: dict[str, Any]
    reverted_at: float | None = None
    reverted_by: str | None = None

    @property
    def active(self) -> bool:
        return self.reverted_at is None

    @property
    def key(self) -> tuple[str, str, str]:
        return self.policy, self.scenario, self.camera


def _calibration(row: sqlite3.Row) -> CalibrationRow:
    return CalibrationRow(
        id=int(row["id"]),
        at=float(row["at"]),
        policy=str(row["policy"]),
        scenario=str(row["scenario"]),
        camera=str(row["camera"]),
        step_from=int(row["step_from"]),
        step_to=int(row["step_to"]),
        bar=None if row["bar"] is None else float(row["bar"]),
        evidence=json.loads(row["evidence"] or "{}"),
        reverted_at=None if row["reverted_at"] is None else float(row["reverted_at"]),
        reverted_by=row["reverted_by"],
    )


def _read_only(path: Path) -> sqlite3.Connection:
    """The database opened for reading only, without migrations: a command-line tool
    that looks at a running server's database never changes it."""
    if not path.is_file():
        raise FileNotFoundError(f"no database at {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def read_events(
    path: Path, *, since_ts: float | None = None, until_ts: float | None = None
) -> list[EventRow]:
    """Events (oldest first) from a database file, read-only (`vidette policy dry-run`,
    `vidette eval --judge stored-live`)."""
    clauses, params = [], []
    if since_ts is not None:
        clauses.append("started_at >= ?")
        params.append(since_ts)
    if until_ts is not None:
        clauses.append("started_at < ?")
        params.append(until_ts)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with contextlib.closing(_read_only(path)) as conn:
        rows = conn.execute(
            f"SELECT * FROM events{where} ORDER BY started_at, id", tuple(params)
        ).fetchall()
    return [_event(row) for row in rows]


def read_calibration(path: Path) -> list[CalibrationRow]:
    """Every calibration step (oldest first), read-only; empty for a database from
    before calibration existed."""
    with contextlib.closing(_read_only(path)) as conn:
        try:
            rows = conn.execute("SELECT * FROM calibration ORDER BY id").fetchall()
        except sqlite3.OperationalError:
            return []
    return [_calibration(row) for row in rows]


def read_alerted_events(path: Path, *, since_ts: float) -> set[str]:
    """Events the Telegram bot sent an alert about (or folded into one), read-only."""
    with contextlib.closing(_read_only(path)) as conn:
        try:
            rows = conn.execute(
                "SELECT DISTINCT event_id FROM telegram_messages WHERE event_id IS NOT NULL"
                " AND kind IN ('alert', 'collapsed') AND created_at >= ?",
                (since_ts,),
            ).fetchall()
        except sqlite3.OperationalError:
            return set()
    return {str(row["event_id"]) for row in rows}


class Database:
    """All methods are safe to call from any task; writes serialize internally."""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self._clock = clock
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        # Optional mirror for system events (the runtime points this at the event bus so
        # M1 subsystems that only know the Database still reach notification rules).
        # Hook failures are logged, never propagated — persistence must not depend on it.
        self.system_event_hook: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None

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
            # FULL (not NORMAL): fsync the WAL on every commit. NORMAL leaves commits in the
            # OS page cache until a checkpoint — a hard VM kill (observed: macOS sleep
            # terminating Docker Desktop's VM) silently discarded them, including the admin
            # account. Our write rate (~1 commit/10 s per camera) makes the fsync cost noise.
            await conn.execute("PRAGMA synchronous=FULL")
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

    async def checkpoint(self) -> None:
        """Move WAL contents into the main DB file (TRUNCATE mode).

        With synchronous=FULL every commit is already crash-durable in the WAL; the
        checkpoint keeps the main file real (a 4 KB .db beside an ever-growing WAL is
        fragile against accidental WAL deletion and confuses backups). The janitor calls
        this every tick; close() calls it on clean shutdown.
        """
        async with self._write_lock:
            with contextlib.suppress(sqlite3.OperationalError):
                await self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    async def close(self) -> None:
        if self._conn is None:
            return
        with contextlib.suppress(Exception):
            await self.checkpoint()
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
        async with self._db.execute("SELECT * FROM users WHERE username = ?", (username,)) as cur:
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
    async def create_api_token(self, name: str, token_hash: str, scopes: str, user_id: int) -> int:
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
        if self.system_event_hook is not None:
            try:
                await self.system_event_hook(kind, payload)
            except Exception:
                logger.exception("system event hook failed for %s", kind)
        return int(row["id"])

    async def recent_system_events(
        self, limit: int = 100, since: float | None = None
    ) -> list[SystemEventRow]:
        """Newest first. `since` keeps only events strictly newer than that timestamp."""
        if since is None:
            sql = "SELECT * FROM system_events ORDER BY at DESC, id DESC LIMIT ?"
            params: tuple[float | int, ...] = (limit,)
        else:
            sql = "SELECT * FROM system_events WHERE at > ? ORDER BY at DESC, id DESC LIMIT ?"
            params = (since, limit)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_system_event(row) for row in rows]

    # --- events (understood events, M2) ---------------------------------------------------
    async def insert_event(
        self,
        event_id: str,
        camera: str,
        started_at: float,
        state: str,
        kinds: list[str],
        zones: list[str],
        geometry: dict[str, Any],
        *,
        ended_at: float | None = None,
        summary: str | None = None,
        intent: dict[str, Any] | None = None,
        policy: str | None = None,
        snapshot_path: str | None = None,
        clip_path: str | None = None,
        source: str | None = None,
        source_ref: str | None = None,
        upstream: dict[str, Any] | None = None,
    ) -> None:
        """Raises ValueError when (source, source_ref) already has a row."""
        async with self._write_lock:
            try:
                await self._db.execute(
                    "INSERT INTO events (id, camera, started_at, ended_at, state, kinds, zones,"
                    " geometry, summary, intent, policy, feedback, snapshot_path, clip_path,"
                    " source, source_ref, upstream)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
                    (
                        event_id,
                        camera,
                        started_at,
                        ended_at,
                        state,
                        json.dumps(kinds),
                        json.dumps(zones),
                        json.dumps(geometry),
                        summary,
                        json.dumps(intent) if intent is not None else None,
                        policy,
                        snapshot_path,
                        clip_path,
                        source,
                        source_ref,
                        json.dumps(upstream) if upstream is not None else None,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"event {source}:{source_ref} already exists") from exc
            await self._db.commit()

    async def update_event(
        self,
        event_id: str,
        *,
        state: str | None = None,
        ended_at: float | None = None,
        snapshot_path: str | None = None,
        clip_path: str | None = None,
        kinds: list[str] | None = None,
        zones: list[str] | None = None,
        upstream: dict[str, Any] | None = None,
        summary: str | None = None,
        intent: dict[str, Any] | None = None,
        policy: str | None = None,
    ) -> None:
        """Partial update: only the provided (non-None) fields change; a no-op otherwise."""
        assignments: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("state", state),
            ("ended_at", ended_at),
            ("snapshot_path", snapshot_path),
            ("clip_path", clip_path),
            ("kinds", None if kinds is None else json.dumps(kinds)),
            ("zones", None if zones is None else json.dumps(zones)),
            ("upstream", None if upstream is None else json.dumps(upstream)),
            ("summary", summary),
            ("intent", None if intent is None else json.dumps(intent)),
            ("policy", policy),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                params.append(value)
        if not assignments:
            return
        params.append(event_id)
        async with self._write_lock:
            await self._db.execute(
                f"UPDATE events SET {', '.join(assignments)} WHERE id = ?", tuple(params)
            )
            await self._db.commit()

    async def set_event_feedback(self, event_id: str, verdict: str | None) -> bool:
        """Record 👍/👎 ('up'/'down') on an event, or clear it (None); False when the id
        is unknown."""
        async with self._write_lock:
            async with self._db.execute(
                "UPDATE events SET feedback = ? WHERE id = ?", (verdict, event_id)
            ) as cur:
                updated = cur.rowcount > 0
            await self._db.commit()
        return updated

    async def get_event(self, event_id: str) -> EventRow | None:
        async with self._db.execute("SELECT * FROM events WHERE id = ?", (event_id,)) as cur:
            row = await cur.fetchone()
        return _event(row) if row is not None else None

    async def get_event_by_source(self, source: str, source_ref: str) -> EventRow | None:
        """The event mirroring one upstream item (e.g. a Frigate review), if any."""
        async with self._db.execute(
            "SELECT * FROM events WHERE source = ? AND source_ref = ?", (source, source_ref)
        ) as cur:
            row = await cur.fetchone()
        return _event(row) if row is not None else None

    async def list_events(
        self,
        *,
        camera: str | None = None,
        since_ts: float | None = None,
        favorite_only: bool = False,
        limit: int = 100,
    ) -> list[EventRow]:
        """Newest first. `since_ts` keeps only events that started strictly after it."""
        clauses: list[str] = []
        params: list[Any] = []
        if camera is not None:
            clauses.append("camera = ?")
            params.append(camera)
        if since_ts is not None:
            clauses.append("started_at > ?")
            params.append(since_ts)
        if favorite_only:
            clauses.append("favorite = 1")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        async with self._db.execute(
            f"SELECT * FROM events{where} ORDER BY started_at DESC, id DESC LIMIT ?",
            tuple(params),
        ) as cur:
            rows = await cur.fetchall()
        return [_event(row) for row in rows]

    async def count_events_by_state(self) -> dict[str, int]:
        """Event counts per lifecycle state (metrics endpoint)."""
        async with self._db.execute(
            "SELECT state, COUNT(*) AS n FROM events GROUP BY state"
        ) as cur:
            rows = await cur.fetchall()
        return {str(row["state"]): int(row["n"]) for row in rows}

    async def set_event_favorite(self, event_id: str, favorite: bool) -> bool:
        async with self._write_lock:
            cursor = await self._db.execute(
                "UPDATE events SET favorite = ? WHERE id = ?", (int(favorite), event_id)
            )
            await self._db.commit()
        return cursor.rowcount > 0

    # --- deep analysis (ADR-0019) -----------------------------------------------------------
    async def set_event_deep(self, event_id: str, deep: dict[str, Any] | None) -> None:
        """The deep analysis of an event, next to its live verdict (never over it)."""
        async with self._write_lock:
            await self._db.execute(
                "UPDATE events SET deep = ? WHERE id = ?",
                (None if deep is None else json.dumps(deep, ensure_ascii=False), event_id),
            )
            await self._db.commit()

    async def deep_enqueue(self, jobs: list[tuple[str, str, float, int, str]]) -> int:
        """Queue events (id, camera, started_at, priority, reason) not queued before; an
        event already in the queue keeps its row, with the better priority. Returns how
        many are new."""
        if not jobs:
            return 0
        now = self._clock()
        async with self._write_lock:
            before = self._db.total_changes
            await self._db.executemany(
                "INSERT INTO deep_jobs (event_id, camera, started_at, priority, reason, state,"
                " queued_at) VALUES (?, ?, ?, ?, ?, 'queued', ?)"
                " ON CONFLICT(event_id) DO UPDATE SET priority = excluded.priority,"
                " reason = excluded.reason"
                " WHERE deep_jobs.state = 'queued' AND excluded.priority < deep_jobs.priority",
                [(e, c, s, p, r, now) for e, c, s, p, r in jobs],
            )
            await self._db.commit()
            changed = self._db.total_changes - before
        return changed

    async def deep_known(self, since_ts: float) -> set[str]:
        """Event ids queued (in any state) for events that started after `since_ts`."""
        async with self._db.execute(
            "SELECT event_id FROM deep_jobs WHERE started_at > ?", (since_ts,)
        ) as cur:
            rows = await cur.fetchall()
        return {str(row["event_id"]) for row in rows}

    async def deep_next(self, *, since_ts: float, max_attempts: int) -> DeepJobRow | None:
        """The next queued job: best priority first, then the newest event."""
        async with self._db.execute(
            "SELECT * FROM deep_jobs WHERE state = 'queued' AND started_at > ? AND attempts < ?"
            " ORDER BY priority, started_at DESC LIMIT 1",
            (since_ts, max_attempts),
        ) as cur:
            row = await cur.fetchone()
        return None if row is None else _deep_job(row)

    async def deep_update(self, event_id: str, **fields: Any) -> None:
        """Set columns of one job (state, attempts, taken_at, done_at, day, error, stats)."""
        allowed = {"state", "attempts", "taken_at", "done_at", "day", "error", "stats"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"not deep_jobs columns: {sorted(unknown)}")
        if not fields:
            return
        values = [
            json.dumps(v, ensure_ascii=False) if k == "stats" and v is not None else v
            for k, v in fields.items()
        ]
        assignments = ", ".join(f"{key} = ?" for key in fields)
        async with self._write_lock:
            await self._db.execute(
                f"UPDATE deep_jobs SET {assignments} WHERE event_id = ?", (*values, event_id)
            )
            await self._db.commit()

    async def deep_requeue_running(self) -> int:
        """Jobs left `running` by a stopped server go back to the queue."""
        async with self._write_lock:
            cursor = await self._db.execute(
                "UPDATE deep_jobs SET state = 'queued', taken_at = NULL WHERE state = 'running'"
            )
            await self._db.commit()
        return cursor.rowcount

    async def deep_counts(self, *, day: str | None = None) -> dict[str, int]:
        """Jobs per state; with `day`, only those that counted against that day's cap."""
        where, params = ("WHERE day = ?", (day,)) if day is not None else ("", ())
        async with self._db.execute(
            f"SELECT state, COUNT(*) AS n FROM deep_jobs {where} GROUP BY state", params
        ) as cur:
            rows = await cur.fetchall()
        return {str(row["state"]): int(row["n"]) for row in rows}

    async def deep_jobs(
        self, *, state: str | None = None, limit: int = 50, done_first: bool = False
    ) -> list[DeepJobRow]:
        """Jobs in queue order (or the most recently finished first)."""
        where, params = ("WHERE state = ?", [state]) if state else ("", [])
        order = "done_at DESC" if done_first else "priority, started_at DESC"
        async with self._db.execute(
            f"SELECT * FROM deep_jobs {where} ORDER BY {order} LIMIT ?", (*params, limit)
        ) as cur:
            rows = await cur.fetchall()
        return [_deep_job(row) for row in rows]

    # --- calibration (ADR-0020) ----------------------------------------------------------------
    async def feedback_events(self, *, since_ts: float) -> list[EventRow]:
        """Events someone marked 👍/👎 that started after `since_ts`, oldest first."""
        async with self._db.execute(
            "SELECT * FROM events WHERE feedback IS NOT NULL AND started_at > ?"
            " ORDER BY started_at, id",
            (since_ts,),
        ) as cur:
            rows = await cur.fetchall()
        return [_event(row) for row in rows]

    async def calibration_steps(self, *, limit: int | None = None) -> list[CalibrationRow]:
        """Every calibration step, oldest first (with `limit`: the newest ones)."""
        sql = "SELECT * FROM calibration ORDER BY id"
        params: tuple[int, ...] = ()
        if limit is not None:
            sql = "SELECT * FROM (SELECT * FROM calibration ORDER BY id DESC LIMIT ?) ORDER BY id"
            params = (limit,)
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_calibration(row) for row in rows]

    async def get_calibration_step(self, step_id: int) -> CalibrationRow | None:
        async with self._db.execute("SELECT * FROM calibration WHERE id = ?", (step_id,)) as cur:
            row = await cur.fetchone()
        return None if row is None else _calibration(row)

    async def add_calibration_step(
        self,
        key: tuple[str, str, str],
        step_from: int,
        step_to: int,
        *,
        bar: float | None,
        evidence: dict[str, Any],
    ) -> CalibrationRow:
        policy, scenario, camera = key
        async with self._write_lock:
            cursor = await self._db.execute(
                "INSERT INTO calibration (at, policy, scenario, camera, step_from, step_to, bar,"
                " evidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (self._clock(), policy, scenario, camera, step_from, step_to, bar,
                 json.dumps(evidence, ensure_ascii=False)),
            )  # fmt: skip
            await self._db.commit()
            step_id = cursor.lastrowid
        found = await self.get_calibration_step(int(step_id or 0))
        assert found is not None
        return found

    async def revert_calibration_step(self, step_id: int, by: str) -> list[CalibrationRow]:
        """Take a step back, and every later step of the same key still in force (they
        were built on it). Returns the steps taken back now; empty when it was not in
        force."""
        step = await self.get_calibration_step(step_id)
        if step is None or not step.active:
            return []
        now = self._clock()
        async with self._write_lock:
            await self._db.execute(
                "UPDATE calibration SET reverted_at = ?, reverted_by = ? WHERE policy = ? AND"
                " scenario = ? AND camera = ? AND id >= ? AND reverted_at IS NULL",
                (now, by, step.policy, step.scenario, step.camera, step.id),
            )
            await self._db.commit()
        async with self._db.execute(
            "SELECT * FROM calibration WHERE policy = ? AND scenario = ? AND camera = ? AND"
            " id >= ? AND reverted_at = ? ORDER BY id",
            (step.policy, step.scenario, step.camera, step.id, now),
        ) as cur:
            rows = await cur.fetchall()
        return [_calibration(row) for row in rows]

    # --- meta (key/value: schema version, VAPID keys, …) -------------------------------------
    async def get_meta(self, key: str) -> str | None:
        async with self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return str(row["value"]) if row is not None else None

    async def set_meta(self, key: str, value: str) -> None:
        async with self._write_lock:
            await self._db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await self._db.commit()

    # --- web-push subscriptions ---------------------------------------------------------------
    async def upsert_push_subscription(
        self, endpoint: str, subscription: dict[str, Any], user_id: int
    ) -> None:
        async with self._write_lock:
            await self._db.execute(
                "INSERT INTO push_subscriptions (endpoint, subscription, user_id, created_at)"
                " VALUES (?, ?, ?, ?) ON CONFLICT(endpoint) DO UPDATE SET"
                " subscription = excluded.subscription, user_id = excluded.user_id",
                (endpoint, json.dumps(subscription), user_id, self._clock()),
            )
            await self._db.commit()

    async def list_push_subscriptions(self) -> list[PushSubscriptionRow]:
        async with self._db.execute("SELECT * FROM push_subscriptions") as cur:
            rows = await cur.fetchall()
        return [_push_subscription(row) for row in rows]

    async def delete_push_subscription(self, endpoint: str) -> bool:
        async with self._write_lock:
            cursor = await self._db.execute(
                "DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,)
            )
            await self._db.commit()
        return cursor.rowcount > 0

    # --- Telegram messages (ADR-0013) -------------------------------------------------------
    async def insert_telegram_message(
        self,
        chat_id: int,
        kind: str,
        *,
        thread_id: int | None = None,
        message_id: int | None = None,
        event_id: str | None = None,
        camera: str | None = None,
        level: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> int:
        """Raises ValueError when this event already has an alert in this chat."""
        async with self._write_lock:
            try:
                cursor = await self._db.execute(
                    "INSERT INTO telegram_messages (chat_id, thread_id, message_id, event_id,"
                    " kind, camera, level, created_at, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (chat_id, thread_id, message_id, event_id, kind, camera, level,
                     self._clock(), json.dumps(data or {})),
                )  # fmt: skip
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"event {event_id} already has a message in this chat") from exc
            await self._db.commit()
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)

    async def update_telegram_message(
        self, row_id: int, *, message_id: int | None = None, data: dict[str, Any] | None = None
    ) -> None:
        assignments: list[str] = []
        params: list[Any] = []
        if message_id is not None:
            assignments.append("message_id = ?")
            params.append(message_id)
        if data is not None:
            assignments.append("data = ?")
            params.append(json.dumps(data))
        if not assignments:
            return
        params.append(row_id)
        async with self._write_lock:
            await self._db.execute(
                f"UPDATE telegram_messages SET {', '.join(assignments)} WHERE id = ?",
                tuple(params),
            )
            await self._db.commit()

    async def delete_telegram_message(self, row_id: int) -> None:
        async with self._write_lock:
            await self._db.execute("DELETE FROM telegram_messages WHERE id = ?", (row_id,))
            await self._db.commit()

    async def get_telegram_message(self, row_id: int) -> TelegramMessageRow | None:
        async with self._db.execute(
            "SELECT * FROM telegram_messages WHERE id = ?", (row_id,)
        ) as cur:
            row = await cur.fetchone()
        return _telegram_message(row) if row is not None else None

    async def last_telegram_alert(
        self,
        chat_id: int | None,
        camera: str | None = None,
        *,
        since: float = 0.0,
        until: float | None = None,
        tests: bool = True,
    ) -> TelegramMessageRow | None:
        """The newest alert (not a collapsed one) in a chat (any chat: None), optionally
        for one camera, sent at or after `since` (and at or before `until`); `tests=False`
        skips test alerts."""
        kinds = "('alert', 'test')" if tests else "('alert')"
        clauses = [f"kind IN {kinds}", "created_at >= ?", "message_id IS NOT NULL"]
        params: list[Any] = [since]
        if until is not None:
            clauses.append("created_at <= ?")
            params.append(until)
        if chat_id is not None:
            clauses.append("chat_id = ?")
            params.append(chat_id)
        if camera is not None:
            clauses.append("camera = ?")
            params.append(camera)
        async with self._db.execute(
            f"SELECT * FROM telegram_messages WHERE {' AND '.join(clauses)}"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            tuple(params),
        ) as cur:
            row = await cur.fetchone()
        return _telegram_message(row) if row is not None else None

    async def telegram_message_at(self, chat_id: int, message_id: int) -> TelegramMessageRow | None:
        """The bot's message row a Telegram message belongs to: the message itself (an
        alert, a collapsed line, an answer), or an alert whose album holds it."""
        async with self._db.execute(
            "SELECT * FROM telegram_messages WHERE chat_id = ? AND message_id = ?"
            " ORDER BY CASE kind WHEN 'alert' THEN 0 ELSE 1 END, id DESC LIMIT 1",
            (chat_id, message_id),
        ) as cur:
            row = await cur.fetchone()
        if row is not None:
            return _telegram_message(row)
        async with self._db.execute(
            "SELECT * FROM telegram_messages WHERE chat_id = ? AND kind IN ('alert', 'test')"
            " ORDER BY id DESC LIMIT 500",
            (chat_id,),
        ) as cur:
            rows = await cur.fetchall()
        for candidate in rows:
            found = _telegram_message(candidate)
            if message_id in (found.data.get("album") or []):
                return found
        return None

    async def telegram_alerts_for_event(self, event_id: str) -> list[TelegramMessageRow]:
        """The alert (or collapsed) messages one event got, one per chat."""
        async with self._db.execute(
            "SELECT * FROM telegram_messages WHERE event_id = ?"
            " AND kind IN ('alert', 'collapsed') ORDER BY id",
            (event_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [_telegram_message(row) for row in rows]

    async def pending_telegram_messages(self) -> list[TelegramMessageRow]:
        """Alerts (and edits of alerts) that wait for Telegram to answer again
        (`data.pending`), the oldest first."""
        async with self._db.execute(
            "SELECT * FROM telegram_messages WHERE kind IN ('alert', 'collapsed')"
            " AND json_extract(data, '$.pending') IS NOT NULL ORDER BY created_at, id"
        ) as cur:
            rows = await cur.fetchall()
        return [_telegram_message(row) for row in rows]

    async def telegram_alerted_events(self, chat_id: int, *, since: float) -> set[str]:
        """Events this chat already heard about as an alert (sent or collapsed)."""
        async with self._db.execute(
            "SELECT event_id FROM telegram_messages WHERE chat_id = ? AND created_at >= ?"
            " AND event_id IS NOT NULL AND kind IN ('alert', 'collapsed', 'test')",
            (chat_id, since),
        ) as cur:
            rows = await cur.fetchall()
        return {str(row["event_id"]) for row in rows}
