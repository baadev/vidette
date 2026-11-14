"""Config schema tests — including the one that keeps deploy/config.example.yaml honest."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from vidette.core.config import (
    Severity,
    VidetteConfig,
    load_config,
    validate_config_text,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "deploy" / "config.example.yaml"

EXAMPLE_ENV = {
    "FRIGATE_MQTT_PASSWORD": "example-password",
    "VIDETTE_WEBHOOK_SECRET": "example-secret",
    "TG_BOT_TOKEN": "123:abc",
    "TG_CHAT_ID": "42",
}

FRIGATE: dict[str, Any] = {"url": "http://frigate:5000", "mqtt": {"host": "mqtt"}}


def test_example_config_is_valid() -> None:
    """The annotated example must always parse — docs, schema and example cannot drift."""
    report = validate_config_text(EXAMPLE.read_text(encoding="utf-8"), env=EXAMPLE_ENV)
    assert report.valid, report.errors
    # The example configures policies on purpose; the not-evaluated-yet warning must fire.
    assert any("backend is none" in w for w in report.warnings)


def test_example_config_contents() -> None:
    config, _ = load_config(EXAMPLE, env=EXAMPLE_ENV)
    assert config.frigate is not None
    assert config.frigate.url == "http://frigate:5000"
    assert config.frigate.mqtt.password == "example-password"  # ${…} interpolated
    assert config.policies and config.policies[0].name == "entry-interest"


def test_frigate_defaults() -> None:
    config = VidetteConfig.model_validate({"frigate": FRIGATE})
    assert config.frigate is not None
    assert config.frigate.mqtt.topic_prefix == "frigate"
    assert config.frigate.mqtt.port == 1883
    assert config.frigate.cameras == "all"
    assert config.frigate.severities == [Severity.alert, Severity.detection]
    assert config.frigate.tracked_objects is True
    assert config.watched_cameras() is None


def test_frigate_url_must_be_http_and_loses_trailing_slash() -> None:
    with pytest.raises(ValueError, match="http://"):
        VidetteConfig.model_validate({"frigate": {**FRIGATE, "url": "frigate:5000"}})
    config = VidetteConfig.model_validate({"frigate": {**FRIGATE, "url": "http://f:5000/"}})
    assert config.frigate is not None and config.frigate.url == "http://f:5000"


def test_frigate_camera_names_use_config_keys_not_friendly_names() -> None:
    with pytest.raises(ValueError, match="friendly_name"):
        VidetteConfig.model_validate({"frigate": {**FRIGATE, "cameras": ["Back Yard"]}})
    config = VidetteConfig.model_validate(
        {"frigate": {**FRIGATE, "cameras": ["back_yard", "front-door"]}}
    )
    assert config.watched_cameras() == ["back_yard", "front-door"]


def test_policy_camera_must_be_watched_when_the_list_is_explicit() -> None:
    with pytest.raises(ValueError, match=r"not in frigate\.cameras"):
        VidetteConfig.model_validate(
            {
                "frigate": {**FRIGATE, "cameras": ["porch"]},
                "policies": [{"name": "p", "description": "d", "cameras": ["garage"]}],
            }
        )
    # With cameras: all, any valid Frigate camera name is accepted.
    VidetteConfig.model_validate(
        {
            "frigate": FRIGATE,
            "policies": [{"name": "p", "description": "d", "cameras": ["garage"]}],
        }
    )


def test_policy_names_are_unique() -> None:
    with pytest.raises(ValueError, match="used twice"):
        VidetteConfig.model_validate(
            {
                "policies": [
                    {"name": "p", "description": "a"},
                    {"name": "p", "description": "b"},
                ]
            }
        )


def test_missing_env_vars_are_named() -> None:
    report = validate_config_text(
        "frigate:\n  url: 'http://${NOT_SET_A}:5000'\n  mqtt: {host: '${NOT_SET_B}'}\n",
        env={},
    )
    assert not report.valid
    assert "NOT_SET_A" in report.errors[0]
    assert "NOT_SET_B" in report.errors[0]


def test_rule_referencing_unknown_channel_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown channel"):
        VidetteConfig.model_validate(
            {"notifications": {"rules": [{"when": "event.confirmed", "channels": ["ghost"]}]}}
        )


def test_webhook_channel_requires_secret() -> None:
    with pytest.raises(ValueError, match="secret"):
        VidetteConfig.model_validate(
            {
                "notifications": {
                    "channels": {"hooks": {"kind": "webhook", "url": "https://example.com"}}
                }
            }
        )


def test_typos_are_errors_not_silent_noops() -> None:
    report = validate_config_text("frigat: {}\n")
    assert not report.valid


def test_v01_nvr_sections_are_rejected_not_ignored() -> None:
    """A v0.1 config must fail loudly on v0.2 instead of silently recording nothing."""
    report = validate_config_text("cameras:\n  front-door:\n    adapter: rtsp\n")
    assert not report.valid
    assert any("cameras" in error for error in report.errors)


def test_auth_none_warns_loudly() -> None:
    report = validate_config_text("server:\n  auth:\n    mode: none\n")
    assert report.valid
    assert any("disables authentication" in w for w in report.warnings)


def test_missing_frigate_warns() -> None:
    report = validate_config_text("{}\n")
    assert report.valid
    assert any("no incident source" in w for w in report.warnings)


def test_v01_vlm_provider_key_points_to_the_new_backends() -> None:
    report = validate_config_text("understanding:\n  vlm:\n    provider: ollama\n")
    assert not report.valid
    assert "backend: none | local | cloud" in report.errors[0]


def test_vlm_backends_local_default_and_cloud_presets() -> None:
    from vidette.core.config import VidetteConfig

    config = VidetteConfig.model_validate({})
    assert config.understanding.vlm.backend == "none"
    assert config.understanding.vlm.local.base_url == "http://localhost:11434/v1"
    text = (
        "understanding:\n  vlm:\n    backend: cloud\n    cloud:\n      preset: gemini\n"
        "      api_key: ${GEMINI_API_KEY}\n"
    )
    report = validate_config_text(text, env={"GEMINI_API_KEY": "k"})
    assert report.valid, report.errors
    assert any("frames from your cameras are sent to Google" in w for w in report.warnings)
    assert any("prices: not set" in w for w in report.warnings)
    cloud_without_section = validate_config_text("understanding:\n  vlm:\n    backend: cloud\n")
    assert not cloud_without_section.valid
    custom_without_url = validate_config_text(
        "understanding:\n  vlm:\n    cloud:\n      preset: custom\n      api_key: k\n"
    )
    assert not custom_without_url.valid and "base_url" in custom_without_url.errors[0]
