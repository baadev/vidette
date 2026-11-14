"""Events REST surface tests: list/get/feedback/favorites/snapshot and Frigate clips.

A bare FastAPI() with only the events router, `app.state.runtime` a SimpleNamespace of
fakes conforming to the DB contract, auth overridden at the `current_principal`
dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import vidette.auth.deps as auth_deps
from vidette.auth.service import ANONYMOUS_ADMIN, Principal
from vidette.core.config import VidetteConfig
from vidette.db import EventRow

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
        self.list_calls: list[tuple[str | None, float | None, bool, int]] = []
        self.updates: list[dict[str, Any]] = []
        self.favorite_calls: list[tuple[str, bool]] = []

    async def list_events(
        self,
        *,
        camera: str | None = None,
        since_ts: float | None = None,
        favorite_only: bool = False,
        limit: int = 100,
    ) -> list[EventRow]:
        self.list_calls.append((camera, since_ts, favorite_only, limit))
        rows = [
            row
            for row in self.rows.values()
            if (camera is None or row.camera == camera)
            and (since_ts is None or row.started_at > since_ts)
            and (not favorite_only or row.favorite)
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

    async def set_event_favorite(self, event_id: str, favorite: bool) -> bool:
        self.favorite_calls.append((event_id, favorite))
        row = self.rows.get(event_id)
        if row is None:
            return False
        self.rows[event_id] = replace(row, favorite=favorite)
        return True

    async def update_event(self, event_id: str, **fields: Any) -> None:
        self.updates.append({"id": event_id, **fields})
        row = self.rows.get(event_id)
        if row is not None:
            changed = {key: value for key, value in fields.items() if value is not None}
            self.rows[event_id] = replace(row, **changed)


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
    app.state.runtime = SimpleNamespace(config=test_config, db=harness.db, frigate_client=None)
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
        "favorite": False,
        "snapshot": None,  # no snapshot on disk → no URL invented
        "clip": None,  # not from Frigate, nothing cached: no clip URL invented
        "source": None,
        "upstream": None,
        "people": None,  # no face check: known people are off
    }
    assert harness.db.list_calls == [(None, None, False, 50)]  # default limit is 50


def test_list_events_passes_filters(harness: Harness) -> None:
    response = harness.client.get(
        "/api/v1/events", params={"camera": "front-door", "since_ts": T0, "limit": 7}
    )
    assert response.status_code == 200
    assert harness.db.list_calls == [("front-door", T0, False, 7)]


def test_list_events_limit_bounds(harness: Harness) -> None:
    assert harness.client.get("/api/v1/events", params={"limit": 0}).status_code == 422
    assert harness.client.get("/api/v1/events", params={"limit": 1001}).status_code == 422


def test_list_events_favorite_filter(harness: Harness) -> None:
    harness.db.rows["ev-0001"] = make_event("ev-0001", started_at=T0)
    harness.db.rows["ev-0002"] = make_event("ev-0002", started_at=T0 + 60, favorite=True)
    response = harness.client.get("/api/v1/events", params={"favorite": "true"})
    assert response.status_code == 200
    body = response.json()
    assert [event["id"] for event in body] == ["ev-0002"]
    assert body[0]["favorite"] is True
    assert harness.db.list_calls == [(None, None, True, 50)]


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


# --- favorites ----------------------------------------------------------------------------------


def test_favorite_star_and_unstar(harness: Harness) -> None:
    harness.db.rows["ev-0001"] = make_event("ev-0001", started_at=T0, ended_at=T0 + 30.0)

    response = harness.client.post("/api/v1/events/ev-0001/favorite", json={"favorite": True})
    assert response.status_code == 204
    assert harness.client.get("/api/v1/events/ev-0001").json()["favorite"] is True

    response = harness.client.post("/api/v1/events/ev-0001/favorite", json={"favorite": False})
    assert response.status_code == 204
    assert harness.db.favorite_calls == [("ev-0001", True), ("ev-0001", False)]
    assert harness.client.get("/api/v1/events/ev-0001").json()["favorite"] is False


def test_favorite_unknown_event_404(harness: Harness) -> None:
    response = harness.client.post("/api/v1/events/ghost/favorite", json={"favorite": True})
    assert response.status_code == 404
    assert _problem(response.json())["title"] == "Event not found"


def test_favorite_bad_body_422(harness: Harness) -> None:
    harness.db.rows["ev-0001"] = make_event("ev-0001")
    assert harness.client.post("/api/v1/events/ev-0001/favorite", json={}).status_code == 422
    response = harness.client.post("/api/v1/events/ev-0001/favorite", json={"favorite": "maybe"})
    assert response.status_code == 422
    assert harness.db.favorite_calls == []
    assert harness.db.rows["ev-0001"].favorite is False


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
    favorite = harness.client.post("/api/v1/events/ev-0001/favorite", json={"favorite": True})
    assert favorite.status_code == 403


# --- clips from Frigate -------------------------------------------------------------------------


class FakeClipClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, float, float, Path]] = []

    async def download_clip(self, camera: str, start: float, end: float, dest: Path) -> Path:
        self.calls.append((camera, start, end, dest))
        if self.error is not None:
            raise self.error
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        return dest


def frigate_event(**overrides: Any) -> EventRow:
    fields: dict[str, Any] = {
        "state": "observed",
        "source": "frigate",
        "source_ref": "1763107200.802000-m2p7xd",
        "upstream": {"severity": "alert", "summary": None},
        "ended_at": T0 + 30.0,
    }
    fields.update(overrides)
    return make_event("ev-0009", **fields)


def test_frigate_event_advertises_a_clip_and_its_upstream(harness: Harness) -> None:
    harness.db.rows["ev-0009"] = frigate_event()
    body = harness.client.get("/api/v1/events/ev-0009").json()
    assert body["clip"] == "/api/v1/events/ev-0009/clip.mp4"
    assert body["source"] == "frigate"
    assert body["upstream"]["severity"] == "alert"


def test_clip_is_fetched_from_frigate_once_then_cached(harness: Harness, media_dir: Path) -> None:
    harness.db.rows["ev-0009"] = frigate_event()
    client = FakeClipClient()
    harness.app.state.runtime.frigate_client = client
    first = harness.client.get("/api/v1/events/ev-0009/clip.mp4")
    assert first.status_code == 200
    assert first.headers["content-type"] == "video/mp4"
    out = media_dir / "clips" / "ev-0009.mp4"
    assert client.calls == [("front-door", T0, T0 + 30.0, out)]
    assert harness.db.rows["ev-0009"].clip_path == str(out)
    assert harness.client.get("/api/v1/events/ev-0009/clip.mp4").status_code == 200
    assert len(client.calls) == 1  # served from the cache


def test_clip_open_event_is_capped(harness: Harness) -> None:
    harness.db.rows["ev-0009"] = frigate_event(ended_at=None)
    client = FakeClipClient()
    harness.app.state.runtime.frigate_client = client
    harness.client.get("/api/v1/events/ev-0009/clip.mp4")
    (_camera, start, end, _dest) = client.calls[0]
    assert end - start <= 300.0


def test_clip_errors_are_explained(harness: Harness) -> None:
    from vidette.frigate.client import FrigateError, FrigateNotFoundError

    harness.db.rows["ev-0009"] = frigate_event()
    harness.app.state.runtime.frigate_client = FakeClipClient(FrigateNotFoundError("404"))
    missing = harness.client.get("/api/v1/events/ev-0009/clip.mp4")
    assert missing.status_code == 404
    assert "enable `record`" in _problem(missing.json())["detail"]

    harness.app.state.runtime.frigate_client = FakeClipClient(FrigateError("cannot reach"))
    down = harness.client.get("/api/v1/events/ev-0009/clip.mp4")
    assert down.status_code == 502
    assert harness.db.rows["ev-0009"].clip_path is None


def test_clip_without_frigate_is_404(harness: Harness) -> None:
    harness.db.rows["ev-0001"] = make_event("ev-0001")
    response = harness.client.get("/api/v1/events/ev-0001/clip.mp4")
    assert response.status_code == 404
    assert _problem(response.json())["title"] == "Clip not available"
