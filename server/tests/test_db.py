"""Tests for the SQLite store (vidette.db)."""

from __future__ import annotations

from pathlib import Path

import pytest

from vidette.db import Database
from vidette.db.schema import SCHEMA_VERSION


class FakeClock:
    """Deterministic, strictly increasing clock for timestamp-sensitive tests."""

    def __init__(self, start: float = 1_000.0, step: float = 1.0) -> None:
        self.now = start
        self.step = step

    def __call__(self) -> float:
        current = self.now
        self.now += self.step
        return current


# --- connect / migrations -----------------------------------------------------------------


async def test_connect_creates_parent_dirs(tmp_path: Path) -> None:
    path = tmp_path / "deeply" / "nested" / "vidette.db"
    database = Database(path)
    await database.connect()
    try:
        assert path.exists()
    finally:
        await database.close()


async def test_migrations_are_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "vidette.db"

    first = Database(path)
    await first.connect()
    await first.connect()  # connect() on a live instance is a no-op
    user_id = await first.create_user("alice", "hash-a")
    await first.close()

    # A fresh process opening the same file must not re-run migrations or lose data.
    second = Database(path)
    await second.connect()
    try:
        user = await second.get_user(user_id)
        assert user is not None and user.username == "alice"
        assert await second.count_users() == 1
    finally:
        await second.close()


async def test_schema_version_recorded(tmp_path: Path) -> None:
    import aiosqlite

    path = tmp_path / "vidette.db"
    database = Database(path)
    await database.connect()
    await database.close()

    async with (
        aiosqlite.connect(path) as conn,
        conn.execute("SELECT value FROM meta WHERE key = 'schema_version'") as cur,
    ):
        row = await cur.fetchone()
    assert row is not None
    assert int(row[0]) == SCHEMA_VERSION


async def test_use_before_connect_is_a_clear_error(tmp_path: Path) -> None:
    database = Database(tmp_path / "vidette.db")
    with pytest.raises(RuntimeError, match="connect"):
        await database.count_users()


# --- users ----------------------------------------------------------------------------------


async def test_create_user_and_lookup(db: Database) -> None:
    assert await db.count_users() == 0
    user_id = await db.create_user("alice", "argon2$abc", role="viewer")
    assert await db.count_users() == 1

    by_name = await db.get_user_by_username("alice")
    by_id = await db.get_user(user_id)
    assert by_name == by_id
    assert by_name is not None
    assert by_name.username == "alice"
    assert by_name.password_hash == "argon2$abc"
    assert by_name.role == "viewer"
    assert by_name.created_at > 0

    assert await db.get_user_by_username("nobody") is None
    assert await db.get_user(user_id + 999) is None


async def test_create_user_duplicate_username(db: Database) -> None:
    await db.create_user("alice", "hash-1")
    with pytest.raises(ValueError, match="username already exists"):
        await db.create_user("alice", "hash-2")
    assert await db.count_users() == 1


# --- sessions -------------------------------------------------------------------------------


async def test_session_create_get_delete_purge(db: Database) -> None:
    user_id = await db.create_user("alice", "hash")
    await db.create_session("tok-live", user_id, expires_at=2_000.0)
    await db.create_session("tok-expired", user_id, expires_at=1_000.0)

    live = await db.get_session("tok-live")
    assert live is not None
    assert live.user_id == user_id
    assert live.expires_at == 2_000.0

    # Expired rows are still returned; expiry policy is the auth service's job.
    assert await db.get_session("tok-expired") is not None
    assert await db.get_session("tok-unknown") is None

    # expires_at == now counts as expired (valid while now < expires_at).
    assert await db.purge_expired_sessions(now=1_000.0) == 1
    assert await db.get_session("tok-expired") is None
    assert await db.get_session("tok-live") is not None

    await db.delete_session("tok-live")
    assert await db.get_session("tok-live") is None


# --- api tokens -----------------------------------------------------------------------------


async def test_api_token_lifecycle(db: Database) -> None:
    user_id = await db.create_user("alice", "hash")
    token_id = await db.create_api_token("ha-bridge", "hash-1", "read:events", user_id)

    token = await db.get_api_token_by_hash("hash-1")
    assert token is not None
    assert token.id == token_id
    assert token.name == "ha-bridge"
    assert token.scopes == "read:events"
    assert token.user_id == user_id
    assert token.last_used_at is None
    assert token.revoked_at is None
    assert await db.get_api_token_by_hash("hash-unknown") is None

    await db.touch_api_token(token_id, now=1_234.0)
    token = await db.get_api_token_by_hash("hash-1")
    assert token is not None and token.last_used_at == 1_234.0

    assert await db.revoke_api_token(token_id, now=2_000.0) is True
    token = await db.get_api_token_by_hash("hash-1")
    assert token is not None and token.revoked_at == 2_000.0

    # Revoking again (or a missing id) reports no change and keeps the original stamp.
    assert await db.revoke_api_token(token_id, now=3_000.0) is False
    assert await db.revoke_api_token(token_id + 999, now=3_000.0) is False
    token = await db.get_api_token_by_hash("hash-1")
    assert token is not None and token.revoked_at == 2_000.0

    assert [t.id for t in await db.list_api_tokens()] == [token_id]


# --- segments -------------------------------------------------------------------------------


async def test_add_segment_upserts_by_path(db: Database) -> None:
    seg_id = await db.add_segment("cam", 0.0, 10.0, "/media/cam/a.mp4", 100)
    again = await db.add_segment(
        "cam", 0.0, 10.5, "/media/cam/a.mp4", 128, klass="motion", codec="h264"
    )
    assert again == seg_id

    segments = await db.all_segments()
    assert len(segments) == 1
    seg = segments[0]
    assert seg.end_ts == 10.5
    assert seg.size_bytes == 128
    assert seg.klass == "motion"
    assert seg.codec == "h264"


async def test_segments_between_half_open_overlap(db: Database) -> None:
    await db.add_segment("cam", 0.0, 10.0, "/m/ends-at-start.mp4", 1)
    await db.add_segment("cam", 5.0, 15.0, "/m/straddles-start.mp4", 1)
    await db.add_segment("cam", 10.0, 20.0, "/m/inside.mp4", 1)
    await db.add_segment("cam", 25.0, 35.0, "/m/straddles-end.mp4", 1)
    await db.add_segment("cam", 30.0, 40.0, "/m/starts-at-end.mp4", 1)
    await db.add_segment("other", 12.0, 18.0, "/m/other-camera.mp4", 1)

    result = await db.segments_between("cam", 10.0, 30.0)
    paths = [seg.path for seg in result]
    # Half-open [10, 30): a segment ending exactly at 10 or starting exactly at 30 is out.
    assert paths == ["/m/straddles-start.mp4", "/m/inside.mp4", "/m/straddles-end.mp4"]


async def test_latest_and_get_segment(db: Database) -> None:
    first = await db.add_segment("cam", 0.0, 10.0, "/m/a.mp4", 1)
    second = await db.add_segment("cam", 10.0, 20.0, "/m/b.mp4", 1)

    latest = await db.latest_segment("cam")
    assert latest is not None and latest.id == second
    assert await db.latest_segment("unknown") is None

    fetched = await db.get_segment(first)
    assert fetched is not None and fetched.path == "/m/a.mp4"
    assert await db.get_segment(second + 999) is None


async def test_hourly_summary_across_two_hours(db: Database) -> None:
    hour0 = 1_700_000 * 3600.0  # exact hour boundary
    hour1 = hour0 + 3600.0
    # Two segments in hour 0, one in hour 1; one out of range; one other camera.
    await db.add_segment("cam", hour0 + 0.0, hour0 + 10.0, "/m/h0-a.mp4", 100)
    await db.add_segment("cam", hour0 + 3590.0, hour0 + 3600.0, "/m/h0-b.mp4", 150)
    await db.add_segment("cam", hour1 + 100.0, hour1 + 110.0, "/m/h1-a.mp4", 200)
    await db.add_segment("cam", hour1 + 3600.0, hour1 + 3610.0, "/m/h2-out.mp4", 999)
    await db.add_segment("other", hour0 + 50.0, hour0 + 60.0, "/m/other.mp4", 999)

    buckets = await db.hourly_summary("cam", hour0, hour1 + 3600.0)
    assert [b.hour_start_ts for b in buckets] == [hour0, hour1]
    assert buckets[0].recorded_seconds == pytest.approx(20.0)
    assert buckets[0].bytes == 250
    assert buckets[1].recorded_seconds == pytest.approx(10.0)
    assert buckets[1].bytes == 200


async def test_hourly_summary_empty_buckets_omitted(db: Database) -> None:
    assert await db.hourly_summary("cam", 0.0, 86_400.0) == []


async def test_delete_segments_by_path_and_media_bytes(db: Database) -> None:
    await db.add_segment("cam", 0.0, 10.0, "/m/a.mp4", 100)
    await db.add_segment("cam", 10.0, 20.0, "/m/b.mp4", 200)
    await db.add_segment("cam", 20.0, 30.0, "/m/c.mp4", 300)
    assert await db.media_bytes_total() == 600

    assert await db.delete_segments_by_path([]) == 0
    deleted = await db.delete_segments_by_path(["/m/a.mp4", "/m/c.mp4", "/m/missing.mp4"])
    assert deleted == 2
    assert [seg.path for seg in await db.all_segments()] == ["/m/b.mp4"]
    assert await db.media_bytes_total() == 200


async def test_media_bytes_total_empty(db: Database) -> None:
    assert await db.media_bytes_total() == 0


# --- system events --------------------------------------------------------------------------


async def test_system_events_order_limit_since(tmp_path: Path) -> None:
    clock = FakeClock(start=100.0, step=1.0)  # events land at 100, 101, 102, 103
    database = Database(tmp_path / "events.db", clock=clock)
    await database.connect()
    try:
        for i in range(4):
            event_id = await database.add_system_event(
                "storage.pressure", {"seq": i, "free_pct": 5.0}
            )
            assert event_id == i + 1

        newest_first = await database.recent_system_events()
        assert [e.payload["seq"] for e in newest_first] == [3, 2, 1, 0]
        assert [e.at for e in newest_first] == [103.0, 102.0, 101.0, 100.0]
        assert newest_first[0].kind == "storage.pressure"
        assert newest_first[0].payload == {"seq": 3, "free_pct": 5.0}

        limited = await database.recent_system_events(limit=2)
        assert [e.payload["seq"] for e in limited] == [3, 2]

        # since is exclusive: the event at exactly `since` is not repeated.
        since = await database.recent_system_events(since=101.0)
        assert [e.at for e in since] == [103.0, 102.0]

        assert await database.recent_system_events(since=103.0) == []
    finally:
        await database.close()
