from __future__ import annotations

from fastapi.testclient import TestClient

from vidette.api.app import create_app


def client() -> TestClient:
    return TestClient(create_app())


def test_healthz() -> None:
    response = client().get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]


def test_system_reports_milestone_and_designed_routes() -> None:
    body = client().get("/api/v1/system").json()
    assert body["milestone"] == "M0"
    assert any(route["path"] == "/api/v1/events" for route in body["designed_routes"])


def test_config_validate_endpoint_accepts_yaml() -> None:
    response = client().post("/api/v1/config/validate", content=b"cameras: {}\n")
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert any("none configured" in warning for warning in body["warnings"])


def test_config_validate_endpoint_rejects_bad_config() -> None:
    response = client().post("/api/v1/config/validate", content=b"cameras: [not, a, map]\n")
    assert response.status_code == 422
    assert response.json()["valid"] is False


def test_designed_routes_are_honest_501s() -> None:
    response = client().get("/api/v1/events")
    assert response.status_code == 501
    body = response.json()
    assert body["status"] == "designed"
    assert body["milestone"] == "M2"


def test_root_serves_status_page_without_web_build() -> None:
    response = client().get("/")
    assert response.status_code == 200
    assert "VIDE" in response.text
