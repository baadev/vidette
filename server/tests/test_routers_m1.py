"""M1 REST surface tests: cameras, recordings + export, streams, system events.

The app under test is a bare FastAPI() with only the M1 routers mounted and
`app.state.runtime` set to a SimpleNamespace of hand-written fakes conforming to the
subsystem contracts (real classes from other modules are never instantiated; only their
exception types and pure `Principal` dataclass are imported, as routers catch/carry those
exact types). Auth is overridden at the `current_principal` dependency, per the deps.py
wiring contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import vidette.auth.deps as auth_deps
from vidette.auth.service import ANONYMOUS_ADMIN, Principal
from vidette.core.config import VidetteConfig
from vidette.recording.exporter import ExportError
from vidette.streams.go2rtc import GatewayError

# --- in-test row/status stand-ins (duck-typed against the DB/recorder contracts) --------------


@dataclass(frozen=True)
class Seg:
    id: int
    camera: str
    start_ts: float
    end_ts: float
    path: str
    size_bytes: int
    klass: str = "continuous"
    codec: str | None = None


@dataclass(frozen=True)
class Bucket:
    hour_start_ts: float
    recorded_seconds: float
    bytes: int


@dataclass(frozen=True)
class Event:
    id: int
    at: float
    kind: str
    payload: dict[str, Any]


# --- fakes -------------------------------------------------------------------------------------


class FakeDb:
    def __init__(self) -> None:
        self.segments: dict[int, Seg] = {}
        self.buckets: list[Bucket] = []
        self.events: list[Event] = []
        self.segments_between_calls: list[tuple[str, float, float]] = []
        self.hourly_calls: list[tuple[str, float, float]] = []
        self.events_calls: list[tuple[int, float | None]] = []

    async def segments_between(self, camera: str, start_ts: float, end_ts: float) -> list[Seg]:
        self.segments_between_calls.append((camera, start_ts, end_ts))
        return sorted(
            (
                seg
                for seg in self.segments.values()
                if seg.camera == camera and seg.end_ts > start_ts and seg.start_ts < end_ts
            ),
            key=lambda seg: seg.start_ts,
        )

    async def get_segment(self, segment_id: int) -> Seg | None:
        return self.segments.get(segment_id)

    async def hourly_summary(
        self, camera: str, day_start_ts: float, day_end_ts: float
    ) -> list[Bucket]:
        self.hourly_calls.append((camera, day_start_ts, day_end_ts))
        return list(self.buckets)

    async def recent_system_events(
        self, limit: int = 100, since: float | None = None
    ) -> list[Event]:
        self.events_calls.append((limit, since))
        return self.events[:limit]


class FakeGateway:
    def __init__(self) -> None:
        self.streams: frozenset[str] = frozenset({"front-door"})
        self.whep_answer = "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=answer\r\n"
        self.whep_error: GatewayError | None = None
        self.snapshot_bytes = b"\xff\xd8\xff\xe0-fake-jpeg"
        self.snapshot_error: GatewayError | None = None
        self.whep_calls: list[tuple[str, str]] = []

    async def health(self) -> SimpleNamespace:
        return SimpleNamespace(reachable=True, version="1.9.9", streams=self.streams, detail="")

    async def whep_exchange(self, camera_id: str, offer_sdp: str) -> str:
        self.whep_calls.append((camera_id, offer_sdp))
        if self.whep_error is not None:
            raise self.whep_error
        return self.whep_answer

    async def snapshot(self, camera_id: str) -> bytes:
        if self.snapshot_error is not None:
            raise self.snapshot_error
        return self.snapshot_bytes


class FakeRecorderSupervisor:
    def __init__(self) -> None:
        self.statuses: dict[str, SimpleNamespace] = {}

    def status(self) -> dict[str, SimpleNamespace]:
        return dict(self.statuses)


class FakeExporter:
    def __init__(self) -> None:
        self.jobs: dict[str, SimpleNamespace] = {}
        self.create_error: ExportError | None = None

    async def create(self, camera: str, start_ts: float, end_ts: float) -> SimpleNamespace:
        if self.create_error is not None:
            raise self.create_error
        job = SimpleNamespace(
            id=f"job-{len(self.jobs) + 1:04d}",
            camera=camera,
            start_ts=start_ts,
            end_ts=end_ts,
            state="queued",
            created_at=0.0,
            path=None,
            error=None,
            size_bytes=None,
        )
        self.jobs[job.id] = job
        return job

    def get(self, job_id: str) -> SimpleNamespace | None:
        return self.jobs.get(job_id)


@dataclass
class Harness:
    client: TestClient
    app: FastAPI
    db: FakeDb = dc_field(default_factory=FakeDb)
    gateway: FakeGateway = dc_field(default_factory=FakeGateway)
    recorder: FakeRecorderSupervisor = dc_field(default_factory=FakeRecorderSupervisor)
    exporter: FakeExporter = dc_field(default_factory=FakeExporter)


def _make_harness(config: VidetteConfig) -> Harness:
    from vidette.api.routers import cameras, recordings, streams, system

    app = FastAPI()
    app.include_router(cameras.router)
    app.include_router(recordings.router)
    app.include_router(streams.router)
    app.include_router(system.router)
    harness = Harness(client=TestClient(app), app=app)
    app.state.runtime = SimpleNamespace(
        config=config,
        db=harness.db,
        auth=SimpleNamespace(),
        go2rtc=harness.gateway,
        recorder=harness.recorder,
        exporter=harness.exporter,
        janitor=SimpleNamespace(),
    )
    app.dependency_overrides[auth_deps.current_principal] = lambda: ANONYMOUS_ADMIN
    return harness


@pytest.fixture
def harness(test_config: VidetteConfig) -> Harness:
    return _make_harness(test_config)


def _problem(body: dict[str, Any]) -> dict[str, Any]:
    detail = body["detail"]
    assert detail["type"] == "about:blank"
    assert isinstance(detail["title"], str) and detail["title"]
    assert isinstance(detail["detail"], str) and detail["detail"]
    return detail  # type: ignore[no-any-return]


# --- cameras -----------------------------------------------------------------------------------


def test_list_cameras_joins_recorder_and_gateway_state(harness: Harness) -> None:
    harness.recorder.statuses["front-door"] = SimpleNamespace(
        camera="front-door",
        state="recording",
        last_segment_at=1751900000.0,
        last_error=None,
        restarts=0,
    )
    response = harness.client.get("/api/v1/cameras")
    assert response.status_code == 200
    assert response.json() == [
        {
            "id": "front-door",
            "name": "front-door",  # falls back to the id — config sets no name
            "adapter": "rtsp",
            "record_mode": "continuous",
            "state": "recording",
            "last_segment_at": 1751900000.0,
            "stream_ready": True,
        }
    ]


def test_list_cameras_idle_when_recorder_and_gateway_silent(harness: Harness) -> None:
    harness.gateway.streams = frozenset()
    body = harness.client.get("/api/v1/cameras").json()
    assert body[0]["state"] == "idle"
    assert body[0]["last_segment_at"] is None
    assert body[0]["stream_ready"] is False


def test_list_cameras_uses_configured_name(test_config: VidetteConfig) -> None:
    config = test_config.model_copy(deep=True)
    config.cameras["front-door"].name = "Front Door"
    harness = _make_harness(config)
    assert harness.client.get("/api/v1/cameras").json()[0]["name"] == "Front Door"


def test_get_camera_includes_probe(harness: Harness) -> None:
    response = harness.client.get("/api/v1/cameras/front-door")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "front-door"
    assert body["stream_ready"] is True
    assert body["probe"]["status"] == "ok"  # rtsp adapter: valid URL syntax
    assert isinstance(body["probe"]["detail"], str) and body["probe"]["detail"]


def test_get_camera_unknown_is_problem_404(harness: Harness) -> None:
    response = harness.client.get("/api/v1/cameras/nope")
    assert response.status_code == 404
    detail = _problem(response.json())
    assert detail["title"] == "Camera not found"
    assert "GET /api/v1/cameras" in detail["detail"]  # tells the user what to do next


# --- recordings --------------------------------------------------------------------------------


def test_list_recordings_returns_segment_shape(harness: Harness) -> None:
    harness.db.segments = {
        1: Seg(1, "front-door", 100.0, 110.0, "/media/x/1.mp4", 111),
        2: Seg(2, "front-door", 110.0, 120.0, "/media/x/2.mp4", 222),
        3: Seg(3, "front-door", 900.0, 910.0, "/media/x/3.mp4", 333),  # outside range
    }
    response = harness.client.get(
        "/api/v1/recordings", params={"camera": "front-door", "from_ts": 90, "to_ts": 130}
    )
    assert response.status_code == 200
    assert response.json() == [
        {"id": 1, "start_ts": 100.0, "end_ts": 110.0, "size_bytes": 111},
        {"id": 2, "start_ts": 110.0, "end_ts": 120.0, "size_bytes": 222},
    ]
    assert harness.db.segments_between_calls == [("front-door", 90.0, 130.0)]


def test_list_recordings_unknown_camera_404(harness: Harness) -> None:
    response = harness.client.get(
        "/api/v1/recordings", params={"camera": "ghost", "from_ts": 0, "to_ts": 10}
    )
    assert response.status_code == 404
    assert _problem(response.json())["title"] == "Camera not found"


def test_list_recordings_range_over_seven_days_422(harness: Harness) -> None:
    response = harness.client.get(
        "/api/v1/recordings",
        params={"camera": "front-door", "from_ts": 0, "to_ts": 7 * 86400 + 1},
    )
    assert response.status_code == 422
    assert "7 days" in _problem(response.json())["detail"]


def test_list_recordings_inverted_range_422(harness: Harness) -> None:
    response = harness.client.get(
        "/api/v1/recordings", params={"camera": "front-door", "from_ts": 50, "to_ts": 50}
    )
    assert response.status_code == 422


def test_recordings_summary_parses_day_as_utc(harness: Harness) -> None:
    harness.db.buckets = [Bucket(hour_start_ts=1783468800.0, recorded_seconds=3599.5, bytes=42)]
    response = harness.client.get(
        "/api/v1/recordings/summary", params={"camera": "front-door", "day": "2026-07-07"}
    )
    assert response.status_code == 200
    assert response.json() == [
        {"hour_start_ts": 1783468800.0, "recorded_seconds": 3599.5, "bytes": 42}
    ]
    day_start = datetime(2026, 7, 7, tzinfo=UTC).timestamp()
    assert harness.db.hourly_calls == [("front-door", day_start, day_start + 86400.0)]


def test_recordings_summary_rejects_bad_day(harness: Harness) -> None:
    response = harness.client.get(
        "/api/v1/recordings/summary", params={"camera": "front-door", "day": "07/07/2026"}
    )
    assert response.status_code == 422
    assert "YYYY-MM-DD" in _problem(response.json())["detail"]


def test_segment_file_serves_mp4(harness: Harness, media_dir: Path) -> None:
    clip = media_dir / "front-door" / "clip.mp4"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"mp4-bytes")
    harness.db.segments[7] = Seg(7, "front-door", 0.0, 10.0, str(clip), 9)
    response = harness.client.get("/api/v1/recordings/segments/7/file")
    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"
    assert response.content == b"mp4-bytes"


def test_segment_file_unknown_row_404(harness: Harness) -> None:
    assert harness.client.get("/api/v1/recordings/segments/99/file").status_code == 404


def test_segment_file_outside_media_dir_404(
    harness: Harness, media_dir: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"secret")  # the file exists — only the path guard stands between
    traversal = str(media_dir / ".." / "outside.mp4")
    harness.db.segments[8] = Seg(8, "front-door", 0.0, 10.0, traversal, 6)
    response = harness.client.get("/api/v1/recordings/segments/8/file")
    assert response.status_code == 404


def test_segment_file_gone_from_disk_404(harness: Harness, media_dir: Path) -> None:
    harness.db.segments[9] = Seg(9, "front-door", 0.0, 10.0, str(media_dir / "gone.mp4"), 1)
    assert harness.client.get("/api/v1/recordings/segments/9/file").status_code == 404


# --- export ------------------------------------------------------------------------------------


def test_create_export_returns_202_job(harness: Harness) -> None:
    response = harness.client.post(
        "/api/v1/export", json={"camera": "front-door", "from_ts": 0, "to_ts": 60}
    )
    assert response.status_code == 202
    assert response.json() == {"id": "job-0001", "state": "queued", "error": None}


def test_create_export_error_is_problem_422(harness: Harness) -> None:
    harness.exporter.create_error = ExportError(
        "no recordings in that range — pick a range that overlaps the timeline"
    )
    response = harness.client.post(
        "/api/v1/export", json={"camera": "front-door", "from_ts": 0, "to_ts": 60}
    )
    assert response.status_code == 422
    detail = _problem(response.json())
    assert detail["detail"].startswith("no recordings in that range")


def test_get_export_reports_download_only_when_done(harness: Harness) -> None:
    harness.client.post("/api/v1/export", json={"camera": "front-door", "from_ts": 0, "to_ts": 9})
    body = harness.client.get("/api/v1/export/job-0001").json()
    assert body == {
        "id": "job-0001",
        "state": "queued",
        "error": None,
        "size_bytes": None,
        "download": None,
    }
    job = harness.exporter.jobs["job-0001"]
    job.state = "done"
    job.size_bytes = 123
    body = harness.client.get("/api/v1/export/job-0001").json()
    assert body["size_bytes"] == 123
    assert body["download"] == "/api/v1/export/job-0001/download"


def test_get_export_unknown_404(harness: Harness) -> None:
    response = harness.client.get("/api/v1/export/nope")
    assert response.status_code == 404
    assert "POST /api/v1/export" in _problem(response.json())["detail"]


def test_download_export_serves_named_mp4(harness: Harness, media_dir: Path) -> None:
    harness.client.post("/api/v1/export", json={"camera": "front-door", "from_ts": 0, "to_ts": 9})
    out = media_dir / "exports" / "job-0001.mp4"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"export-bytes")
    job = harness.exporter.jobs["job-0001"]
    job.state = "done"
    job.path = out
    job.size_bytes = len(b"export-bytes")
    response = harness.client.get("/api/v1/export/job-0001/download")
    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"
    assert "vidette-front-door-job-0001.mp4" in response.headers["content-disposition"]
    assert response.content == b"export-bytes"


def test_download_export_unfinished_or_unknown_404(harness: Harness) -> None:
    harness.client.post("/api/v1/export", json={"camera": "front-door", "from_ts": 0, "to_ts": 9})
    assert harness.client.get("/api/v1/export/job-0001/download").status_code == 404  # queued
    assert harness.client.get("/api/v1/export/absent/download").status_code == 404


# --- streams -----------------------------------------------------------------------------------


def test_stream_info_urls(harness: Harness) -> None:
    response = harness.client.get("/api/v1/streams/front-door")
    assert response.status_code == 200
    assert response.json() == {
        "webrtc": "/api/v1/streams/front-door/whep",
        "mse": "/api/v1/streams/front-door/mse",
        "snapshot": "/api/v1/streams/front-door/snapshot.jpeg",
    }


def test_stream_info_unknown_camera_404(harness: Harness) -> None:
    response = harness.client.get("/api/v1/streams/ghost")
    assert response.status_code == 404
    assert _problem(response.json())["title"] == "Camera not found"


def test_whep_proxies_sdp(harness: Harness) -> None:
    offer = "v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\ns=offer\r\n"
    response = harness.client.post(
        "/api/v1/streams/front-door/whep",
        content=offer.encode(),
        headers={"content-type": "application/sdp"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/sdp")
    assert response.text == harness.gateway.whep_answer
    assert harness.gateway.whep_calls == [("front-door", offer)]


def test_whep_gateway_failure_is_problem_502(harness: Harness) -> None:
    harness.gateway.whep_error = GatewayError(
        "go2rtc is unreachable at http://go2rtc:1984 — check the sidecar container"
    )
    response = harness.client.post(
        "/api/v1/streams/front-door/whep",
        content=b"v=0\r\n",
        headers={"content-type": "application/sdp"},
    )
    assert response.status_code == 502
    detail = _problem(response.json())
    assert detail["title"] == "Stream gateway error"
    assert "go2rtc is unreachable" in detail["detail"]


def test_whep_empty_body_422(harness: Harness) -> None:
    response = harness.client.post("/api/v1/streams/front-door/whep", content=b"")
    assert response.status_code == 422
    assert "SDP offer" in _problem(response.json())["detail"]


def test_whep_unknown_camera_404(harness: Harness) -> None:
    response = harness.client.post("/api/v1/streams/ghost/whep", content=b"v=0\r\n")
    assert response.status_code == 404
    assert harness.gateway.whep_calls == []  # validated before touching the gateway


def test_snapshot_returns_jpeg(harness: Harness) -> None:
    response = harness.client.get("/api/v1/streams/front-door/snapshot.jpeg")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == harness.gateway.snapshot_bytes


def test_snapshot_gateway_failure_is_problem_502(harness: Harness) -> None:
    harness.gateway.snapshot_error = GatewayError(
        "go2rtc has no stream 'front-door' — run POST /api/v1/config/apply or restart"
    )
    response = harness.client.get("/api/v1/streams/front-door/snapshot.jpeg")
    assert response.status_code == 502
    assert _problem(response.json())["title"] == "Stream gateway error"


# --- system events -----------------------------------------------------------------------------


def test_system_events_shape_and_limit(harness: Harness) -> None:
    harness.db.events = [
        Event(id=2, at=200.0, kind="storage.low", payload={"free_fraction": 0.12}),
        Event(id=1, at=100.0, kind="recorder.stalled", payload={"camera": "front-door"}),
    ]
    response = harness.client.get("/api/v1/system/events", params={"limit": 2})
    assert response.status_code == 200
    assert response.json() == [
        {"at": 200.0, "kind": "storage.low", "payload": {"free_fraction": 0.12}},
        {"at": 100.0, "kind": "recorder.stalled", "payload": {"camera": "front-door"}},
    ]
    assert harness.db.events_calls == [(2, None)]


def test_system_events_limit_bounds(harness: Harness) -> None:
    assert harness.client.get("/api/v1/system/events", params={"limit": 0}).status_code == 422


# --- auth guard --------------------------------------------------------------------------------


def test_scope_guard_rejects_viewer_without_read_streams(harness: Harness) -> None:
    viewer = Principal(
        user_id=2,
        username="viewer",
        role="viewer",
        scopes=frozenset({"read:events"}),
        via="session",
    )
    harness.app.dependency_overrides[auth_deps.current_principal] = lambda: viewer
    for path in (
        "/api/v1/cameras",
        "/api/v1/recordings?camera=front-door&from_ts=0&to_ts=1",
        "/api/v1/streams/front-door",
        "/api/v1/system/events",
    ):
        assert harness.client.get(path).status_code == 403, path
