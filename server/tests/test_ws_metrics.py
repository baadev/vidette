"""Live WebSocket stream (`/api/v1/ws`) + Prometheus `/metrics` tests (M2).

Harness notes (same family as tests/test_events_api.py):
- A bare FastAPI() with only the ws + metrics routers; `app.state.runtime` is a
  SimpleNamespace mirroring the AppRuntime attributes the routers touch — a REAL
  InProcessEventBus, a real tmp-file Database, fakes for the worker `status()` shapes.
- `with TestClient(app) as client:` keeps ONE portal event loop shared by the websocket
  session, HTTP requests and `client.portal.call(...)` — so bus queue wakeups and
  aiosqlite futures all live on a single loop (asyncio primitives are loop-affine).
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from vidette import __version__
from vidette.api.routers.metrics import router as metrics_router
from vidette.api.routers.ws import router as ws_router
from vidette.core.config import VidetteConfig
from vidette.core.events import InProcessEventBus
from vidette.db import Database
from vidette.notify.dispatcher import DispatcherStatus
from vidette.pipeline.runner import PipelineStatus
from vidette.recording.janitor import JanitorStatus
from vidette.recording.recorder import CameraRecorderStatus

T0 = 1_751_900_000.0


def make_config(tmp_path: Path, *, auth_mode: str = "none") -> VidetteConfig:
    return VidetteConfig.model_validate(
        {
            "server": {"auth": {"mode": auth_mode}},
            "storage": {
                "media_dir": str(tmp_path / "media"),
                "database": str(tmp_path / "vidette.db"),
            },
        }
    )


class FakeAuth:
    """AuthService stand-in: fixed answers, calls recorded."""

    def __init__(self, bearer: Any = None, session: Any = None) -> None:
        self._bearer = bearer
        self._session = session
        self.bearer_calls: list[str] = []
        self.session_calls: list[str] = []

    async def authenticate_bearer(self, token: str) -> Any:
        self.bearer_calls.append(token)
        return self._bearer

    async def authenticate_session(self, session_token: str) -> Any:
        self.session_calls.append(session_token)
        return self._session


def make_runtime(
    config: VidetteConfig,
    *,
    db: Database | None = None,
    auth: FakeAuth | None = None,
    janitor_status: JanitorStatus | None = None,
) -> SimpleNamespace:
    pipelines = {
        "front-door": PipelineStatus(
            camera="front-door",
            state="running",
            frames_total=1234,
            motion_frames=56,
            detect_calls=7,
            last_frame_at=T0,
            last_error=None,
            restarts=0,
        ),
        "yard": PipelineStatus(
            camera="yard",
            state="backoff",
            frames_total=10,
            motion_frames=2,
            detect_calls=0,
            last_frame_at=None,
            last_error="decode failed",
            restarts=3,
        ),
    }
    recorders = {
        "front-door": CameraRecorderStatus(
            camera="front-door",
            state="recording",
            last_segment_at=T0,
            last_error=None,
            restarts=1,
        ),
        "yard": CameraRecorderStatus(
            camera="yard",
            state="backoff",
            last_segment_at=None,
            last_error="ffmpeg exited",
            restarts=9,
        ),
    }
    janitor = janitor_status or JanitorStatus(
        last_run_at=T0,
        disk_total_bytes=500_000_000_000,
        disk_free_bytes=123_456_789,
        media_bytes=42_000_000,
        last_probe_ok=True,
        expired_deleted_total=3,
        pressure_deleted_total=1,
    )
    notifier = DispatcherStatus(delivered_total=11, failed_total=2, per_channel={})
    return SimpleNamespace(
        config=config,
        db=db,
        auth=auth,
        bus=InProcessEventBus(),
        detector_state="ready",
        pipeline=SimpleNamespace(status=lambda: pipelines),
        recorder=SimpleNamespace(status=lambda: recorders),
        janitor=SimpleNamespace(status=lambda: janitor),
        notifier=SimpleNamespace(status=lambda: notifier),
    )


def make_app(runtime: SimpleNamespace) -> FastAPI:
    app = FastAPI()
    app.include_router(ws_router)
    app.include_router(metrics_router)
    app.state.runtime = runtime
    return app


def run_on_app_loop(client: TestClient, func: Callable[..., Awaitable[Any]], *args: Any) -> Any:
    """Run a coroutine function on the TestClient's portal loop (the loop the app is on)."""
    assert client.portal is not None  # set while the TestClient context is entered
    return client.portal.call(func, *args)


# --- websocket: live stream ---------------------------------------------------------------------


def test_ws_streams_published_events_default_topics(tmp_path: Path) -> None:
    runtime = make_runtime(make_config(tmp_path))
    app = make_app(runtime)
    with TestClient(app) as client, client.websocket_connect("/api/v1/ws") as ws:
        run_on_app_loop(
            client, runtime.bus.publish, "event.confirmed", {"id": "ev-0001", "camera": "yard"}
        )
        assert ws.receive_json() == {
            "topic": "event.confirmed",
            "payload": {"id": "ev-0001", "camera": "yard"},
        }
        # The default subscription also carries system.* topics.
        run_on_app_loop(client, runtime.bus.publish, "system.storage.pressure", {"deleted": 2})
        assert ws.receive_json() == {
            "topic": "system.storage.pressure",
            "payload": {"deleted": 2},
        }


def test_ws_topics_filter_drops_unsubscribed_topics(tmp_path: Path) -> None:
    runtime = make_runtime(make_config(tmp_path))
    app = make_app(runtime)
    with TestClient(app) as client, client.websocket_connect("/api/v1/ws?topics=event.*") as ws:
        # Both publishes complete before we read (portal.call blocks until publish returns).
        # Had system.* been subscribed, its frame would arrive first — receiving the event
        # frame first proves the system message was never delivered.
        run_on_app_loop(client, runtime.bus.publish, "system.storage.pressure", {"deleted": 2})
        run_on_app_loop(client, runtime.bus.publish, "event.observed", {"id": "ev-0002"})
        assert ws.receive_json() == {"topic": "event.observed", "payload": {"id": "ev-0002"}}


@pytest.mark.parametrize("topics", ["weather.sunny", "event", "*", "event.*,bogus.*", ""])
def test_ws_invalid_topics_closes_4400(tmp_path: Path, topics: str) -> None:
    runtime = make_runtime(make_config(tmp_path))
    app = make_app(runtime)
    with TestClient(app) as client:
        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect(f"/api/v1/ws?topics={topics}") as ws,
        ):
            ws.receive_text()
        assert excinfo.value.code == 4400
    assert runtime.bus._subscriptions == []  # nothing was subscribed, nothing leaked


def test_ws_unauthenticated_builtin_closes_4401_without_accept(tmp_path: Path) -> None:
    runtime = make_runtime(make_config(tmp_path, auth_mode="builtin"), auth=FakeAuth())
    app = make_app(runtime)
    with TestClient(app) as client:
        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            client.websocket_connect("/api/v1/ws"),
        ):
            pass  # the close arrives before accept, so entering raises
        assert excinfo.value.code == 4401
    assert runtime.auth is not None
    assert runtime.auth.bearer_calls == []  # no credentials were even presented
    assert runtime.auth.session_calls == []


def test_ws_bearer_token_accepted_under_builtin_auth(tmp_path: Path) -> None:
    from vidette.auth.service import Principal

    principal = Principal(
        user_id=7,
        username="ops",
        role="viewer",
        scopes=frozenset({"read:events"}),
        via="token",
    )
    auth = FakeAuth(bearer=principal)
    runtime = make_runtime(make_config(tmp_path, auth_mode="builtin"), auth=auth)
    app = make_app(runtime)
    with (
        TestClient(app) as client,
        client.websocket_connect("/api/v1/ws", headers={"Authorization": "Bearer vd_secret"}) as ws,
    ):
        run_on_app_loop(client, runtime.bus.publish, "event.confirmed", {"id": "ev-0003"})
        assert ws.receive_json()["topic"] == "event.confirmed"
    assert auth.bearer_calls == ["vd_secret"]


def test_ws_subscriptions_closed_after_disconnect(tmp_path: Path) -> None:
    runtime = make_runtime(make_config(tmp_path))
    app = make_app(runtime)
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/ws?topics=event.*,system.*") as ws:
            run_on_app_loop(client, runtime.bus.publish, "event.confirmed", {"id": "ev-0004"})
            assert ws.receive_json()["topic"] == "event.confirmed"
            assert len(runtime.bus._subscriptions) == 2
        # Context exit disconnects the client; give the endpoint a loop turn to clean up.
        run_on_app_loop(client, runtime.bus.publish, "event.confirmed", {"id": "ev-0005"})
    assert runtime.bus._subscriptions == []


# --- /metrics ------------------------------------------------------------------------------------


def test_metrics_exposition(tmp_path: Path) -> None:
    db = Database(tmp_path / "vidette.db")
    runtime = make_runtime(make_config(tmp_path), db=db)
    runtime.bus.dropped = 4
    app = make_app(runtime)
    with TestClient(app) as client:
        run_on_app_loop(client, db.connect)
        try:
            run_on_app_loop(
                client,
                functools.partial(
                    db.insert_event,
                    "ev-0001",
                    "front-door",
                    T0,
                    "confirmed",
                    ["person"],
                    ["door"],
                    {"touch": True},
                ),
            )
            run_on_app_loop(
                client,
                functools.partial(
                    db.insert_event, "ev-0002", "yard", T0 + 60, "dismissed", ["vehicle"], [], {}
                ),
            )
            response = client.get("/metrics")
        finally:
            run_on_app_loop(client, db.close)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    body = response.text
    assert body.endswith("\n")
    lines = body.splitlines()

    assert f'vidette_info{{version="{__version__}"}} 1' in lines
    assert "vidette_detector_ready 1" in lines

    assert 'vidette_pipeline_up{camera="front-door"} 1' in lines
    assert 'vidette_pipeline_up{camera="yard"} 0' in lines
    assert 'vidette_pipeline_frames_total{camera="front-door"} 1234' in lines
    assert 'vidette_pipeline_motion_frames_total{camera="front-door"} 56' in lines
    assert 'vidette_pipeline_detect_calls_total{camera="front-door"} 7' in lines
    assert 'vidette_recorder_up{camera="front-door"} 1' in lines
    assert 'vidette_recorder_up{camera="yard"} 0' in lines
    assert 'vidette_recorder_restarts_total{camera="yard"} 9' in lines

    assert "vidette_disk_total_bytes 500000000000" in lines
    assert "vidette_disk_free_bytes 123456789" in lines
    assert "vidette_media_bytes 42000000" in lines
    assert "vidette_janitor_expired_deleted_total 3" in lines
    assert "vidette_janitor_pressure_deleted_total 1" in lines
    assert "vidette_storage_probe_ok 1" in lines

    assert "vidette_notifications_delivered_total 11" in lines
    assert "vidette_notifications_failed_total 2" in lines
    assert "vidette_bus_dropped_total 4" in lines

    # Real rows behind the counts; absent states are emitted as explicit zeros.
    assert 'vidette_events_total{state="confirmed"} 1' in lines
    assert 'vidette_events_total{state="dismissed"} 1' in lines
    assert 'vidette_events_total{state="observed"} 0' in lines
    assert 'vidette_events_total{state="analyzing"} 0' in lines

    # Exposition hygiene: HELP/TYPE headers present for key families.
    assert "# TYPE vidette_pipeline_up gauge" in lines
    assert "# TYPE vidette_pipeline_frames_total counter" in lines
    assert "# HELP vidette_bus_dropped_total " in body


def test_metrics_unknown_janitor_values_emit_no_samples(tmp_path: Path) -> None:
    before_first_run = JanitorStatus(
        last_run_at=None,
        disk_total_bytes=None,
        disk_free_bytes=None,
        media_bytes=None,
        last_probe_ok=None,
        expired_deleted_total=0,
        pressure_deleted_total=0,
    )
    db = Database(tmp_path / "vidette.db")
    runtime = make_runtime(make_config(tmp_path), db=db, janitor_status=before_first_run)
    app = make_app(runtime)
    with TestClient(app) as client:
        run_on_app_loop(client, db.connect)
        try:
            response = client.get("/metrics")
        finally:
            run_on_app_loop(client, db.close)

    body = response.text
    assert response.status_code == 200
    assert "NaN" not in body
    unknown = (
        "vidette_disk_total_bytes",
        "vidette_disk_free_bytes",
        "vidette_media_bytes",
        "vidette_storage_probe_ok",
    )
    for name in unknown:
        samples = [line for line in body.splitlines() if line.startswith(f"{name} ")]
        assert samples == []  # headers only — no sample line for an unknown value
        assert f"# TYPE {name} gauge" in body


def test_metrics_requires_auth_under_builtin(tmp_path: Path) -> None:
    runtime = make_runtime(make_config(tmp_path, auth_mode="builtin"), auth=FakeAuth())
    app = make_app(runtime)
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 401
