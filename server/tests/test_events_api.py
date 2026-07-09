"""Events REST surface tests: list/get/feedback/snapshot + the lazy clip path.

Same pattern as tests/test_routers_m1.py: a bare FastAPI() with only the events router,
`app.state.runtime` a SimpleNamespace of fakes conforming to the DB contract, auth
overridden at the `current_principal` dependency. The lazy-clip round trip uses real
ffmpeg over two tiny generated segments (skipped when ffmpeg is absent).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import FFMPEG, requires_ffmpeg
from fastapi import FastAPI
from fastapi.testclient import TestClient

import vidette.auth.deps as auth_deps
from vidette.auth.service import ANONYMOUS_ADMIN, Principal
from vidette.core.config import VidetteConfig
from vidette.db import EventRow, SegmentRow

T0 = 1_751_900_000.0


def make_event(
    event_id: str = "ev-0001",
    *,
    camera: str = "front-door",
    started_at: float = T0,
    **overrides: Any,
) -> EventRow:
    base: dict[str, Any] = {
        "id": event_id,
        "camera": camera,
        "started_at": started_at,
        "ended_at": None,
        "state": "confirmed",
        "kinds": ["person"],
        "zones": ["door"],
        "geometry": {
            "approach": 0.92,
            "dwell_s": 14.2,
            "touch": True,
            "loiter": False,
            "repeat_pass": 0,
        },
        "summary": None,
        "intent": None,
        "policy": "default",
        "feedback": None,
        "snapshot_path": None,
        "clip_path": None,
    }
    base.update(overrides)
    return EventRow(**base)


class FakeDb:
    def __init__(self) -> None:
        self.rows: dict[str, EventRow] = {}
        self.segments: list[SegmentRow] = []
        self.list_calls: list[tuple[str | None, float | None, int]] = []
        self.updates: list[dict[str, Any]] = []

    async def list_events(
        self,
        *,
        camera: str | None = None,
        since_ts: float | None = None,
        limit: int = 100,
    ) -> list[EventRow]:
        self.list_calls.append((camera, since_ts, limit))
        rows = [
            row
            for row in self.rows.values()
            if (camera is None or row.camera == camera)
            and (since_ts is None or row.started_at > since_ts)
        ]
        rows.sort(key=lambda row: (row.started_at, row.id), reverse=True)
        return rows[:limit]

    async def get_event(self, event_id: str) -> EventRow | None:
        return self.rows.get(event_id)

    async def set_event_feedback(self, event_id: str, verdict: str) -> bool:
        row = self.rows.get(event_id)
        if row is None:
            return False
        self.rows[event_id] = replace(row, feedback=verdict)
        return True

    async def update_event(self, event_id: str, **fields: Any) -> None:
        self.updates.append({"id": event_id, **fields})
        row = self.rows.get(event_id)
        if row is not None:
            changed = {key: value for key, value in fields.items() if value is not None}
            self.rows[event_id] = replace(row, **changed)

    async def segments_between(
        self, camera: str, start_ts: float, end_ts: float
    ) -> list[SegmentRow]:
        return sorted(
            (
                row
                for row in self.segments
                if row.camera == camera and row.end_ts > start_ts and row.start_ts < end_ts
            ),
            key=lambda row: row.start_ts,
        )


@dataclass
class Harness:
    client: TestClient
    app: FastAPI
    db: FakeDb = field(default_factory=FakeDb)


@pytest.fixture
def harness(test_config: VidetteConfig) -> Harness:
    from vidette.api.routers import events

    app = FastAPI()
    app.include_router(events.router)
    harness = Harness(client=TestClient(app), app=app)
    app.state.runtime = SimpleNamespace(config=test_config, db=harness.db)
    app.dependency_overrides[auth_deps.current_principal] = lambda: ANONYMOUS_ADMIN
    return harness


def _problem(body: dict[str, Any]) -> dict[str, Any]:
    detail = body["detail"]
    assert detail["type"] == "about:blank"
    assert isinstance(detail["title"], str) and detail["title"]
    assert isinstance(detail["detail"], str) and detail["detail"]
    return detail  # type: ignore[no-any-return]


# --- list / get ---------------------------------------------------------------------------------


def test_list_events_newest_first_full_shape(harness: Harness) -> None:
    harness.db.rows["ev-0001"] = make_event("ev-0001", started_at=T0)
    harness.db.rows["ev-0002"] = make_event(
        "ev-0002", started_at=T0 + 60, ended_at=T0 + 90, state="dismissed", policy=None
    )
    response = harness.client.get("/api/v1/events")
    assert response.status_code == 200
    body = response.json()
    assert [event["id"] for event in body] == ["ev-0002", "ev-0001"]
    assert body[1] == {
        "id": "ev-0001",
        "camera": "front-door",
        "started_at": T0,
        "ended_at": None,
        "state": "confirmed",
        "kinds": ["person"],
        "zones": ["door"],
        "geometry": {
            "approach": 0.92,
            "dwell_s": 14.2,
            "touch": True,
            "loiter": False,
            "repeat_pass": 0,
        },
        "summary": None,
        "policy": "default",
        "feedback": None,
        "snapshot": None,  # no snapshot on disk → no URL invented
        "clip": "/api/v1/events/ev-0001/clip.mp4",
    }
    assert harness.db.list_calls == [(None, None, 50)]  # default limit is 50


def test_list_events_passes_filters(harness: Harness) -> None:
    response = harness.client.get(
        "/api/v1/events", params={"camera": "front-door", "since_ts": T0, "limit": 7}
    )
    assert response.status_code == 200
    assert harness.db.list_calls == [("front-door", T0, 7)]


def test_list_events_limit_bounds(harness: Harness) -> None:
    assert harness.client.get("/api/v1/events", params={"limit": 0}).status_code == 422
    assert harness.client.get("/api/v1/events", params={"limit": 1001}).status_code == 422


def test_get_event_includes_snapshot_url_when_present(harness: Harness, media_dir: Path) -> None:
    snap = media_dir / "front-door" / "events" / "ev-0001" / "snapshot.jpeg"
    harness.db.rows["ev-0001"] = make_event("ev-0001", snapshot_path=str(snap))
    body = harness.client.get("/api/v1/events/ev-0001").json()
    assert body["id"] == "ev-0001"
    assert body["snapshot"] == "/api/v1/events/ev-0001/snapshot.jpeg"


def test_get_event_unknown_404(harness: Harness) -> None:
    response = harness.client.get("/api/v1/events/ghost")
    assert response.status_code == 404
    detail = _problem(response.json())
    assert detail["title"] == "Event not found"
    assert "GET /api/v1/events" in detail["detail"]


# --- feedback -----------------------------------------------------------------------------------


def test_feedback_records_verdict(harness: Harness) -> None:
    harness.db.rows["ev-0001"] = make_event("ev-0001")
    response = harness.client.post("/api/v1/events/ev-0001/feedback", json={"verdict": "up"})
    assert response.status_code == 204
    assert harness.db.rows["ev-0001"].feedback == "up"


def test_feedback_bad_verdict_422(harness: Harness) -> None:
    harness.db.rows["ev-0001"] = make_event("ev-0001")
    response = harness.client.post("/api/v1/events/ev-0001/feedback", json={"verdict": "sideways"})
    assert response.status_code == 422
    assert harness.db.rows["ev-0001"].feedback is None


def test_feedback_unknown_event_404(harness: Harness) -> None:
    response = harness.client.post("/api/v1/events/ghost/feedback", json={"verdict": "down"})
    assert response.status_code == 404


# --- snapshot -----------------------------------------------------------------------------------


def test_snapshot_serves_jpeg(harness: Harness, media_dir: Path) -> None:
    snap = media_dir / "front-door" / "events" / "ev-0001" / "snapshot.jpeg"
    snap.parent.mkdir(parents=True)
    snap.write_bytes(b"\xff\xd8\xff\xe0-jpeg")
    harness.db.rows["ev-0001"] = make_event("ev-0001", snapshot_path=str(snap))
    response = harness.client.get("/api/v1/events/ev-0001/snapshot.jpeg")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == b"\xff\xd8\xff\xe0-jpeg"


def test_snapshot_absent_404(harness: Harness) -> None:
    harness.db.rows["ev-0001"] = make_event("ev-0001")  # no snapshot_path
    response = harness.client.get("/api/v1/events/ev-0001/snapshot.jpeg")
    assert response.status_code == 404
    assert _problem(response.json())["title"] == "Snapshot not available"


def test_snapshot_missing_file_404(harness: Harness, media_dir: Path) -> None:
    gone = media_dir / "front-door" / "events" / "ev-0001" / "snapshot.jpeg"
    harness.db.rows["ev-0001"] = make_event("ev-0001", snapshot_path=str(gone))
    assert harness.client.get("/api/v1/events/ev-0001/snapshot.jpeg").status_code == 404


def test_snapshot_outside_media_dir_404(harness: Harness, media_dir: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.jpeg"
    outside.write_bytes(b"secret")  # exists — only the path guard stands between
    traversal = str(media_dir / ".." / "outside.jpeg")
    harness.db.rows["ev-0001"] = make_event("ev-0001", snapshot_path=traversal)
    assert harness.client.get("/api/v1/events/ev-0001/snapshot.jpeg").status_code == 404


# --- clip (lazy materialization) ----------------------------------------------------------------


def make_segment_clip(path: Path, seconds: float = 2.0) -> None:
    assert FFMPEG is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            FFMPEG,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=320x240:rate=10:duration={seconds}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(path),
        ],
        check=True,
        timeout=60,
    )


def seg_row(row_id: int, start_ts: float, path: Path, seconds: float = 2.0) -> SegmentRow:
    return SegmentRow(
        id=row_id,
        camera="front-door",
        start_ts=start_ts,
        end_ts=start_ts + seconds,
        path=str(path),
        size_bytes=path.stat().st_size if path.exists() else 0,
        klass="continuous",
        codec="h264",
    )


@requires_ffmpeg
def test_clip_lazy_materialization_roundtrip(harness: Harness, media_dir: Path) -> None:
    seg_dir = media_dir / "front-door" / "2026" / "07" / "07" / "12"
    clip_a = seg_dir / f"{int(T0)}.mp4"
    clip_b = seg_dir / f"{int(T0) + 2}.mp4"
    make_segment_clip(clip_a)
    make_segment_clip(clip_b)
    harness.db.segments = [seg_row(1, T0, clip_a), seg_row(2, T0 + 2.0, clip_b)]
    harness.db.rows["ev-0001"] = make_event("ev-0001", started_at=T0 + 1.0, ended_at=T0 + 3.0)

    response = harness.client.get("/api/v1/events/ev-0001/clip.mp4")
    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"
    out = media_dir / "front-door" / "events" / "ev-0001" / "clip.mp4"
    assert out.is_file()
    assert response.content == out.read_bytes()
    assert harness.db.updates == [{"id": "ev-0001", "clip_path": str(out)}]
    assert harness.db.rows["ev-0001"].clip_path == str(out)

    # Second request serves the cached file without another materialization.
    again = harness.client.get("/api/v1/events/ev-0001/clip.mp4")
    assert again.status_code == 200
    assert len(harness.db.updates) == 1


def test_clip_cached_path_served_directly(harness: Harness, media_dir: Path) -> None:
    cached = media_dir / "front-door" / "events" / "ev-0001" / "clip.mp4"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"cached-mp4")
    harness.db.rows["ev-0001"] = make_event("ev-0001", clip_path=str(cached))
    response = harness.client.get("/api/v1/events/ev-0001/clip.mp4")
    assert response.status_code == 200
    assert response.content == b"cached-mp4"
    assert harness.db.updates == []


def test_clip_no_footage_404(harness: Harness) -> None:
    harness.db.rows["ev-0001"] = make_event("ev-0001", ended_at=T0 + 5.0)  # no segments
    response = harness.client.get("/api/v1/events/ev-0001/clip.mp4")
    assert response.status_code == 404
    detail = _problem(response.json())
    assert detail["title"] == "Clip not available"
    assert "no recorded footage for this event yet" in detail["detail"]


def test_clip_segment_outside_media_dir_404(
    harness: Harness, media_dir: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"not ours")
    harness.db.segments = [seg_row(1, T0, outside)]
    harness.db.rows["ev-0001"] = make_event("ev-0001", ended_at=T0 + 2.0)
    response = harness.client.get("/api/v1/events/ev-0001/clip.mp4")
    assert response.status_code == 404
    assert not (media_dir / "front-door" / "events" / "ev-0001" / "clip.mp4").exists()


def test_clip_unknown_event_404(harness: Harness) -> None:
    assert harness.client.get("/api/v1/events/ghost/clip.mp4").status_code == 404


# --- auth guard ---------------------------------------------------------------------------------


def test_scope_guard_requires_read_events(harness: Harness) -> None:
    streams_only = Principal(
        user_id=2,
        username="viewer",
        role="viewer",
        scopes=frozenset({"read:streams"}),
        via="session",
    )
    harness.app.dependency_overrides[auth_deps.current_principal] = lambda: streams_only
    assert harness.client.get("/api/v1/events").status_code == 403
    assert harness.client.get("/api/v1/events/ev-0001").status_code == 403
