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

_V3_MANAGEMENT: Final[str] = """
-- UI-managed cameras: the hand-written YAML stays the IaC source of truth; cameras created
-- from the web UI live here and are merged at load (YAML wins on id collision).
CREATE TABLE managed_cameras (
    id         TEXT PRIMARY KEY,   -- same [a-z0-9-] rules as YAML camera ids
    config     TEXT NOT NULL,      -- JSON of a validated CameraConfig
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- Web-push subscriptions (VAPID); endpoint is the natural unique key.
CREATE TABLE push_subscriptions (
    endpoint     TEXT PRIMARY KEY,
    subscription TEXT NOT NULL,    -- full JSON subscription object from the browser
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at   REAL NOT NULL
);

-- Event favorites: pinned events survive every cleanup; their footage is upgraded to the
-- 'favorite' segment class so retention never eats it.
ALTER TABLE events ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0;
"""

_V4_UPSTREAM: Final[str] = """
-- v0.2 (ADR-0009): events come from an upstream NVR. `source`/`source_ref` identify the
-- upstream item (Frigate review id) so every phase of one incident lands on one row;
-- `upstream` keeps what the upstream said (severity, its own GenAI summary) as JSON.
-- The v0.1 tables `segments` and `managed_cameras` stay (append-only) but are unused.
ALTER TABLE events ADD COLUMN source TEXT;
ALTER TABLE events ADD COLUMN source_ref TEXT;
ALTER TABLE events ADD COLUMN upstream TEXT;
CREATE UNIQUE INDEX idx_events_source_ref ON events(source, source_ref)
    WHERE source IS NOT NULL;
"""

_V5_TELEGRAM: Final[str] = """
-- The Telegram bot (ADR-0013): one row per message it sent about events. An alert row
-- carries the event it is about; `data` holds what is needed to answer its buttons and
-- edit its text (caption lines, the mark, album message ids). The partial unique index
-- makes "one alert per event and chat" a database fact, not a hope (test alerts, sent by
-- `vidette telegram try`, may repeat).
CREATE TABLE telegram_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    thread_id   INTEGER,
    message_id  INTEGER,
    event_id    TEXT,
    kind        TEXT NOT NULL,   -- alert | collapsed | digest | test
    camera      TEXT,
    level       TEXT,
    created_at  REAL NOT NULL,
    data        TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX idx_telegram_alert_once ON telegram_messages(event_id, chat_id)
    WHERE kind IN ('alert', 'collapsed');
CREATE INDEX idx_telegram_chat_camera ON telegram_messages(chat_id, camera, created_at);
"""

_V6_DEEP: Final[str] = """
-- Deep analysis (ADR-0019): a second, slower look at an event by the big model, kept
-- next to the live verdict (`deep`, JSON), never over it; and the queue it works from.
-- One row per event, so a clip is analysed once however often it is queued; `running`
-- rows are put back to `queued` when the server starts (the request died with it).
ALTER TABLE events ADD COLUMN deep TEXT;
CREATE TABLE deep_jobs (
    event_id    TEXT PRIMARY KEY,
    camera      TEXT NOT NULL,
    started_at  REAL NOT NULL,   -- the event's
    priority    INTEGER NOT NULL, -- lower goes first
    reason      TEXT NOT NULL,   -- why it is worth a look: alert, level, person, vehicle, …
    state       TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | failed | skipped
    attempts    INTEGER NOT NULL DEFAULT 0,
    queued_at   REAL NOT NULL,
    taken_at    REAL,
    done_at     REAL,
    day         TEXT,            -- the local day it counted against the daily cap
    error       TEXT,
    stats       TEXT             -- JSON: calls, seconds per call, tokens, slot checks, …
);
CREATE INDEX idx_deep_jobs_state ON deep_jobs(state, priority, started_at);
CREATE INDEX idx_deep_jobs_day ON deep_jobs(day);
"""

_V7_CALIBRATION: Final[str] = """
-- Calibration (ADR-0020): 👍/👎 move one scenario on one camera a step quieter (a
-- confidence bar, then the digest) or back. One row per step, never deleted: a row taken
-- back keeps when and by whom (`reverted_*`), so every change stays auditable. The
-- effective step of a key is `step_to` of its newest row not taken back (0 without one).
CREATE TABLE calibration (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          REAL NOT NULL,
    policy      TEXT NOT NULL,
    scenario    TEXT NOT NULL,
    camera      TEXT NOT NULL,
    step_from   INTEGER NOT NULL,
    step_to     INTEGER NOT NULL,  -- 0 as written, 1 a confidence bar, 2 the digest only
    bar         REAL,              -- step 1: wakes only from this confidence
    evidence    TEXT NOT NULL,     -- JSON: the marks that made it (counts, reasons, events)
    reverted_at REAL,
    reverted_by TEXT               -- web | telegram | cli | mark (a 👍 on what it hid)
);
CREATE INDEX idx_calibration_key ON calibration(policy, scenario, camera, id);
CREATE INDEX idx_events_feedback ON events(feedback) WHERE feedback IS NOT NULL;
"""

MIGRATIONS: Final[list[str]] = [
    _V1_INITIAL,
    _V2_EVENTS,
    _V3_MANAGEMENT,
    _V4_UPSTREAM,
    _V5_TELEGRAM,
    _V6_DEEP,
    _V7_CALIBRATION,
]

SCHEMA_VERSION: Final[int] = len(MIGRATIONS)
