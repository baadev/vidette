"""Schema DDL for the Vidette SQLite store (ADR-0008).

`MIGRATIONS` is **append-only**: each entry is one migration step, executed with
``executescript`` in order. `Database.connect()` reads ``meta(key='schema_version')``,
applies every migration past the recorded version, and bumps the version after each step.
Never edit or reorder shipped entries — add a new DDL string at the end instead.

The ``meta`` table itself is created by ``Database.connect()`` (it must exist before the
version can be read), so it is deliberately not part of any migration.
"""

from __future__ import annotations

from typing import Final

_V1_INITIAL: Final[str] = """
CREATE TABLE users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL DEFAULT 'admin',
    created_at    REAL    NOT NULL
);

CREATE TABLE sessions (
    token_hash TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL    NOT NULL,
    expires_at REAL    NOT NULL
);

CREATE INDEX idx_sessions_expires_at ON sessions(expires_at);

CREATE TABLE api_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    token_hash   TEXT    NOT NULL UNIQUE,
    scopes       TEXT    NOT NULL,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at   REAL    NOT NULL,
    last_used_at REAL,
    revoked_at   REAL
);

CREATE TABLE segments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    camera     TEXT    NOT NULL,
    start_ts   REAL    NOT NULL,
    end_ts     REAL    NOT NULL,
    path       TEXT    NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL,
    klass      TEXT    NOT NULL DEFAULT 'continuous',
    codec      TEXT
);

CREATE INDEX idx_segments_camera_start_ts ON segments(camera, start_ts);

CREATE TABLE system_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      REAL    NOT NULL,
    kind    TEXT    NOT NULL,
    payload TEXT    NOT NULL
);

CREATE INDEX idx_system_events_at ON system_events(at);
"""

_V2_EVENTS: Final[str] = """
CREATE TABLE events (
    id            TEXT PRIMARY KEY,
    camera        TEXT NOT NULL,
    started_at    REAL NOT NULL,
    ended_at      REAL,
    state         TEXT NOT NULL,
    kinds         TEXT NOT NULL,  -- JSON array, e.g. ["person"]
    zones         TEXT NOT NULL,  -- JSON array of zone names
    geometry      TEXT NOT NULL,  -- JSON object (GeometryFacts shape)
    summary       TEXT,           -- Tier 3 text; NULL without/before a VLM (M3)
    intent        TEXT,           -- JSON object (IntentVerdict shape); NULL until M3
    policy        TEXT,
    feedback      TEXT,           -- 'up' | 'down' | NULL
    snapshot_path TEXT,
    clip_path     TEXT
);

CREATE INDEX idx_events_camera_started_at ON events(camera, started_at);
CREATE INDEX idx_events_started_at ON events(started_at);
"""

MIGRATIONS: Final[list[str]] = [
    _V1_INITIAL,
    _V2_EVENTS,
]

SCHEMA_VERSION: Final[int] = len(MIGRATIONS)
