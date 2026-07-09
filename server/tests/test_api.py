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


def test_system_reports_milestone_and_runtime_state(client: TestClient) -> None:
    body = client.get("/api/v1/system").json()
    assert body["milestone"] == "M2"
    assert "test-mode" in body["config_warnings"]
    assert body["gateway"]["reachable"] is False  # no gateway in tests — reported honestly
    assert body["detector"] in ("loading", "ready", "disabled")
    assert any(route["path"] == "/api/v1/policies" for route in body["designed_routes"])


def test_config_validate_endpoint_accepts_yaml(client: TestClient) -> None:
    response = client.post("/api/v1/config/validate", content=b"cameras: {}\n")
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert any("none configured" in warning for warning in body["warnings"])


def test_config_validate_endpoint_rejects_bad_config(client: TestClient) -> None:
    response = client.post("/api/v1/config/validate", content=b"cameras: [not, a, map]\n")
    assert response.status_code == 422
    assert response.json()["valid"] is False


def test_designed_routes_are_honest_501s(client: TestClient) -> None:
    policies = client.get("/api/v1/policies")
    assert policies.status_code == 501
    assert policies.json()["milestone"] == "M4"
    # /api/v1/events shipped with M2 core — it must NOT be a 501 anymore.
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
        assert test_client.get("/api/v1/cameras").status_code == 401
        assert test_client.post("/api/v1/config/validate", content=b"{}").status_code == 401
