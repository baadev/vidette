"""FrigateClient against an httpx MockTransport that serves the recorded API fixtures."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from conftest import FIXTURES

from vidette.core.incidents import Incident
from vidette.frigate.client import (
    FrigateClient,
    FrigateError,
    FrigateNotFoundError,
    frame_times,
)

API = FIXTURES / "frigate" / "api"
JPEG = (API / "snapshot.jpg").read_bytes()
BASE = "http://frigate.test:5000"


class FakeFrigate:
    """Routes a handful of Frigate API paths to fixture files; records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.recordings = True  # False → recording frames 404 (camera does not record)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/version":
            return httpx.Response(200, text=(API / "version.txt").read_text())
        if path == "/api/config":
            return httpx.Response(200, content=(API / "config.json").read_bytes())
        if path == "/api/review":
            return httpx.Response(200, content=(API / "reviews.json").read_bytes())
        if path.startswith("/api/review/"):
            if path.endswith("/missing"):
                return httpx.Response(404, json={"success": False})
            return httpx.Response(200, content=(API / "review.json").read_bytes())
        if path.startswith("/api/events/") and path.endswith("/snapshot.jpg"):
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        if "/recordings/" in path and path.endswith("/snapshot.jpg"):
            if not self.recordings:
                return httpx.Response(404, json={"success": False})
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        if path.endswith("/clip.mp4"):
            return httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42" + b"x" * 4096)
        return httpx.Response(404)


def client_for(fake: Callable[[httpx.Request], httpx.Response]) -> FrigateClient:
    return FrigateClient(BASE, transport=httpx.MockTransport(fake))


def incident(**overrides: object) -> Incident:
    base: dict[str, object] = {
        "source": "frigate",
        "source_id": "1763107200.802000-m2p7xd",
        "camera": "front_door",
        "started_at": 1763107200.0,
        "ended_at": 1763107220.0,
        "detections": ["e1", "e2"],
    }
    base.update(overrides)
    return Incident.model_validate(base)


async def test_version_config_and_cameras() -> None:
    async with client_for(FakeFrigate()) as client:
        assert await client.version() == "0.18.2-1f0c2ab"
        assert await client.cameras() == ["front_door", "street"]


async def test_review_and_reviews() -> None:
    fake = FakeFrigate()
    async with client_for(fake) as client:
        review = await client.review("1763107200.802000-m2p7xd")
        assert review.camera == "front_door"
        assert review.data.metadata is not None
        history = await client.reviews(
            cameras=["front_door", "street"], severity="alert", after=1.0, limit=10
        )
        assert [item.camera for item in history] == ["front_door", "street"]
        with pytest.raises(FrigateNotFoundError):
            await client.review("missing")
    query = fake.requests[1].url.params
    assert query["cameras"] == "front_door,street"
    assert query["severity"] == "alert"
    assert query["limit"] == "10"


async def test_event_snapshot_and_recording_frame_paths() -> None:
    fake = FakeFrigate()
    async with client_for(fake) as client:
        assert await client.event_snapshot("e1", crop=True, height=240) == JPEG
        assert await client.recording_frame("front_door", 1763107205.5) == JPEG
    snap, frame = fake.requests
    assert snap.url.path == "/api/events/e1/snapshot.jpg"
    assert snap.url.params["crop"] == "1" and snap.url.params["height"] == "240"
    assert frame.url.path == "/api/front_door/recordings/1763107205.5/snapshot.jpg"


def test_frame_times_spread_inside_the_incident() -> None:
    assert frame_times(100.0, 120.0, 3) == [105.0, 110.0, 115.0]
    assert frame_times(100.0, None, 1) == [105.0]  # open incident: first 10 s
    assert frame_times(100.0, 120.0, 0) == []


async def test_incident_frames_prefer_recordings() -> None:
    async with client_for(FakeFrigate()) as client:
        frames = await client.incident_frames(incident(), count=3)
    assert [frame.origin for frame in frames] == ["recording"] * 3
    assert [frame.at for frame in frames] == [1763107205.0, 1763107210.0, 1763107215.0]


async def test_incident_frames_fall_back_to_event_snapshots() -> None:
    fake = FakeFrigate()
    fake.recordings = False
    async with client_for(fake) as client:
        frames = await client.incident_frames(incident(), count=3)
    assert [frame.origin for frame in frames] == ["event_snapshot", "event_snapshot"]


async def test_download_clip_is_atomic(tmp_path: Path) -> None:
    fake = FakeFrigate()
    dest = tmp_path / "clips" / "a.mp4"
    async with client_for(fake) as client:
        await client.download_clip("front_door", 1.0, 2.5, dest)
    assert dest.read_bytes().startswith(b"\x00\x00\x00\x18ftyp")
    assert not dest.with_suffix(".mp4.part").exists()
    assert fake.requests[0].url.path == "/api/front_door/start/1.0/end/2.5/clip.mp4"


async def test_unreachable_frigate_is_an_actionable_error() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with client_for(refuse) as client:
        with pytest.raises(FrigateError, match=r"frigate\.url"):
            await client.version()


async def test_authenticated_port_is_explained() -> None:
    async with client_for(lambda request: httpx.Response(401)) as client:
        with pytest.raises(FrigateError, match="5000"):
            await client.cameras()


def test_cli_check_reports_version_and_cameras(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from vidette.frigate import cli

    monkeypatch.setattr(
        cli,
        "FrigateClient",
        lambda url: FrigateClient(url, transport=httpx.MockTransport(FakeFrigate())),
    )
    assert cli.cmd_check(None, BASE) == 0
    out = capsys.readouterr().out
    assert "Frigate 0.18.2-1f0c2ab" in out
    assert "front_door, street" in out


def test_cli_check_flags_old_frigate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from vidette.frigate import cli

    def old(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, text="0.13.2-6476f8a")
        return FakeFrigate()(request)

    monkeypatch.setattr(
        cli, "FrigateClient", lambda url: FrigateClient(url, transport=httpx.MockTransport(old))
    )
    assert cli.cmd_check(None, BASE) == 1
    assert "0.14 or newer" in capsys.readouterr().out
