from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vidette.api.app import create_app
from vidette.core.config import VidetteConfig
from vidette.runtime import AppRuntime


def _config(tmp_path: Path, auth_mode: str) -> VidetteConfig:
    return VidetteConfig.model_validate(
        {
            "server": {"auth": {"mode": auth_mode}},
            "storage": {
                "media_dir": str(tmp_path / "media"),
                "database": str(tmp_path / "vidette.db"),
            },
        }
    )


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("VIDETTE_WEB_DIST", str(tmp_path / "no-web-dist"))
    runtime = AppRuntime(_config(tmp_path, "none"), config_warnings=["test-mode"])
    app = create_app(runtime, workers=False)
    with TestClient(app) as test_client:
        yield test_client


def test_healthz_is_public(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_system_reports_stage_and_runtime_state(client: TestClient) -> None:
    body = client.get("/api/v1/system").json()
    assert body["milestone"].startswith("v0.2")
    assert "test-mode" in body["config_warnings"]
    assert any(route["path"] == "/api/v1/policies" for route in body["designed_routes"])
    # The NVR core is frozen (ADR-0009): no recorder/gateway/pipeline state is reported.
    for gone in ("gateway", "recorders", "keepwarm", "detector", "pipelines"):
        assert gone not in body


def test_config_validate_endpoint_accepts_yaml(client: TestClient) -> None:
    response = client.post("/api/v1/config/validate", content=b"policies: []\n")
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert any("no incident source" in warning for warning in body["warnings"])


def test_config_validate_endpoint_rejects_bad_config(client: TestClient) -> None:
    response = client.post("/api/v1/config/validate", content=b"policies: {not: a list}\n")
    assert response.status_code == 422
    assert response.json()["valid"] is False


def test_designed_routes_are_honest_501s(client: TestClient) -> None:
    policies = client.get("/api/v1/policies")
    assert policies.status_code == 501
    assert policies.json()["milestone"] == "v0.2"
    # /api/v1/events is real: it must NOT be a 501.
    events = client.get("/api/v1/events")
    assert events.status_code == 200


def test_root_serves_status_page_without_web_build(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "VIDE" in response.text


def test_auth_builtin_gates_the_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDETTE_WEB_DIST", str(tmp_path / "no-web-dist"))
    runtime = AppRuntime(_config(tmp_path, "builtin"))
    app = create_app(runtime, workers=False)
    with TestClient(app) as test_client:
        assert test_client.get("/healthz").status_code == 200  # probes stay public
        assert test_client.get("/api/v1/system").status_code == 401
        assert test_client.get("/api/v1/events").status_code == 401
        assert test_client.post("/api/v1/config/validate", content=b"{}").status_code == 401


def test_frozen_nvr_routes_are_gone(client: TestClient) -> None:
    """ADR-0009: recording, streams and camera management left the v0.2 line."""
    for path in ("/api/v1/cameras", "/api/v1/recordings", "/api/v1/config/cameras"):
        assert client.get(path).status_code == 404, path


def test_system_reports_the_frigate_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDETTE_WEB_DIST", str(tmp_path / "no-web-dist"))
    config = _config(tmp_path, "none").model_copy(
        update={
            "frigate": VidetteConfig.model_validate(
                {"frigate": {"url": "http://frigate:5000", "mqtt": {"host": "mqtt"}}}
            ).frigate
        }
    )
    app = create_app(AppRuntime(config), workers=False)
    with TestClient(app) as test_client:
        frigate = test_client.get("/api/v1/system").json()["frigate"]
    assert frigate["url"] == "http://frigate:5000"
    assert frigate["broker"] == "mqtt:1883"
    assert frigate["topics"] == ["frigate/available", "frigate/reviews", "frigate/events"]
    assert frigate["connected"] is False  # workers off: never connected, said so
    assert frigate["available"] is None


def test_system_frigate_is_null_without_a_frigate_section(client: TestClient) -> None:
    assert client.get("/api/v1/system").json()["frigate"] is None


def test_frigate_incidents_reach_the_events_api_through_the_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wiring check: the real AppRuntime with workers on, a recorded Frigate session fed
    to its source, and the events API answering with one observed event."""
    import asyncio

    from conftest import FIXTURES

    from vidette.frigate.source import replay

    monkeypatch.setenv("VIDETTE_WEB_DIST", str(tmp_path / "no-web-dist"))
    config = VidetteConfig.model_validate(
        {
            "server": {"auth": {"mode": "none"}},
            "storage": {
                "media_dir": str(tmp_path / "media"),
                "database": str(tmp_path / "vidette.db"),
            },
            "frigate": {"url": "http://frigate.invalid:5000", "mqtt": {"host": "mqtt.invalid"}},
        }
    )
    runtime = AppRuntime(config)
    assert runtime.frigate_source is not None

    class IdleBroker:
        """Never connects anywhere: the test feeds the source directly."""

        async def __aenter__(self) -> IdleBroker:
            await asyncio.Event().wait()
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(runtime.frigate_source, "_client_factory", IdleBroker)
    monkeypatch.setattr(runtime, "frigate_client", None)  # no HTTP: snapshots skipped
    runtime.intake._snapshots = None

    app = create_app(runtime, workers=True)
    with TestClient(app) as test_client:
        session = FIXTURES / "frigate" / "session-front-door.jsonl"
        test_client.portal.call(replay, runtime.frigate_source, session)
        events: list[dict[str, object]] = []
        for _ in range(100):
            events = test_client.get("/api/v1/events").json()
            if events and events[0]["ended_at"] is not None:
                break
            test_client.portal.call(asyncio.sleep, 0.02)
        system = test_client.get("/api/v1/system").json()
    assert len(events) == 1
    event = events[0]
    assert event["state"] == "observed"
    assert event["source"] == "frigate"
    assert event["camera"] == "front_door"
    assert event["clip"] == f"/api/v1/events/{event['id']}/clip.mp4"
    assert system["frigate"]["incidents_total"] == 1
