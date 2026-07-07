"""Config schema tests — including the one that keeps deploy/config.example.yaml honest."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from vidette.core.config import (
    VidetteConfig,
    load_config,
    parse_duration,
    validate_config_text,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "deploy" / "config.example.yaml"

EXAMPLE_ENV = {
    "CAM_PASSWORD": "example-password",
    "VIDETTE_WEBHOOK_SECRET": "example-secret",
    "TG_BOT_TOKEN": "123:abc",
    "TG_CHAT_ID": "42",
}


def test_example_config_is_valid() -> None:
    """The annotated example must always parse — docs, schema and example cannot drift."""
    report = validate_config_text(EXAMPLE.read_text(encoding="utf-8"), env=EXAMPLE_ENV)
    assert report.valid, report.errors
    # The example configures design-stage features on purpose; honesty warnings must fire.
    assert any("M1" in w for w in report.warnings)


def test_example_config_contents() -> None:
    config, warnings = load_config(EXAMPLE, env=EXAMPLE_ENV)
    assert "front-door" in config.cameras
    front_door = config.cameras["front-door"]
    assert front_door.source is not None
    assert "example-password" in front_door.source.main  # ${CAM_PASSWORD} interpolated
    assert front_door.zones["street"].kind.value == "public"
    assert config.cameras["backyard"].adapter == "eufy"
    assert any("eufy" in w for w in warnings)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("90s", timedelta(seconds=90)),
        ("30m", timedelta(minutes=30)),
        ("12h", timedelta(hours=12)),
        ("3d", timedelta(days=3)),
        ("forever", None),
    ],
)
def test_parse_duration(raw: str, expected: timedelta | None) -> None:
    assert parse_duration(raw) == expected


def test_parse_duration_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration("3 days")


def test_missing_env_vars_are_named() -> None:
    report = validate_config_text(
        "cameras:\n  cam:\n    source: {main: 'rtsp://u:${NOT_SET_A}@h/1?${NOT_SET_B}'}\n",
        env={},
    )
    assert not report.valid
    assert "NOT_SET_A" in report.errors[0]
    assert "NOT_SET_B" in report.errors[0]


def test_zone_needs_three_normalized_points() -> None:
    base = {
        "cameras": {
            "cam": {
                "source": {"main": "rtsp://h/1"},
                "zones": {"door": {"kind": "entry", "points": [[0.1, 0.1], [0.2, 0.2]]}},
            }
        }
    }
    with pytest.raises(ValueError):
        VidetteConfig.model_validate(base)

    base["cameras"]["cam"]["zones"]["door"]["points"] = [[0.1, 0.1], [0.2, 0.2], [1.5, 0.3]]
    with pytest.raises(ValueError, match="normalized"):
        VidetteConfig.model_validate(base)


def test_policy_referencing_unknown_camera_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown camera"):
        VidetteConfig.model_validate(
            {"policies": [{"name": "p", "description": "d", "cameras": ["ghost"]}]}
        )


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
    report = validate_config_text("camers: {}\n")
    assert not report.valid


def test_auth_none_warns_loudly() -> None:
    report = validate_config_text("server:\n  auth:\n    mode: none\n")
    assert report.valid
    assert any("disables authentication" in w for w in report.warnings)


def test_trusted_faces_enabled_warns_designed() -> None:
    report = validate_config_text("understanding:\n  faces:\n    enabled: true\n")
    assert report.valid
    assert any("trusted-faces" in w and "M4" in w for w in report.warnings)
