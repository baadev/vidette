"""Frigate payload parsing and the mapping to Vidette's incident model."""

from __future__ import annotations

import json

import pytest
from conftest import FIXTURES
from pydantic import ValidationError

from vidette.core.incidents import Incident, Phase
from vidette.frigate.models import (
    FrigateEventMessage,
    FrigateReview,
    FrigateReviewMessage,
    object_phase,
    to_incident,
    to_tracked_object,
)
from vidette.frigate.source import iter_recording

SESSION = FIXTURES / "frigate" / "session-front-door.jsonl"


def _messages(topic_suffix: str) -> list[bytes]:
    return [raw for topic, raw in iter_recording(SESSION) if topic.endswith(topic_suffix)]


def test_review_messages_parse_and_map_to_incidents() -> None:
    new, update, end, genai = (
        FrigateReviewMessage.model_validate_json(raw) for raw in _messages("/reviews")
    )
    assert [m.type for m in (new, update, end, genai)] == ["new", "update", "end", "genai"]

    incident = to_incident(update.after)
    assert incident.source == "frigate"
    assert incident.key == f"frigate:{update.after.id}"
    assert incident.camera == "front_door"
    assert incident.severity == "alert"
    assert incident.objects == ["person"]
    assert incident.zones == ["path", "porch"]
    assert incident.detections == ["1763107200.412000-k3v9qa"]
    assert incident.ended_at is None and incident.duration_s is None
    assert incident.upstream_summary is None

    closed = to_incident(end.after)
    assert closed.duration_s == pytest.approx(32.698, abs=1e-3)

    summarized = to_incident(genai.after)
    summary = summarized.upstream_summary
    assert summary is not None
    assert summary.title == "Person tries the front door"
    assert summary.text is not None and summary.text.startswith("A person walked up")
    assert summary.threat_level == 1
    assert summary.confidence == 0.8


def test_tracked_object_messages_parse() -> None:
    new, _update, update2, end = (
        FrigateEventMessage.model_validate_json(raw) for raw in _messages("/events")
    )
    assert object_phase(new.type) is Phase.opened
    assert object_phase(end.type) is Phase.closed
    assert object_phase("genai") is None

    obj = to_tracked_object(update2.after)
    assert obj.label == "person"
    assert obj.current_zones == ["porch"]
    assert obj.entered_zones == ["path", "porch"]
    assert obj.stationary is True and obj.motionless_count == 58
    assert obj.box == [424.0, 500.0, 536.0, 712.0]
    assert to_tracked_object(end.after).ended_at is not None


def test_unknown_fields_are_ignored_and_missing_optionals_default() -> None:
    """Frigate adds fields in minor releases; that must not break parsing."""
    review = FrigateReview.model_validate(
        {
            "id": "r1",
            "camera": "porch",
            "start_time": 1.0,
            "severity": "alert",
            "brand_new_field": {"nested": True},
            "data": {"objects": ["person"], "something_new": 3, "zones": None},
        }
    )
    assert review.end_time is None
    assert review.data.zones == []  # null treated as empty
    assert review.data.detections == []


def test_review_times_accept_epoch_numbers_strings_and_iso_datetimes() -> None:
    """MQTT sends epoch floats; Frigate's HTTP schema declares date-time strings."""
    base = {"id": "r", "camera": "c", "severity": "detection"}
    as_float = FrigateReview.model_validate({**base, "start_time": 1763107200.5})
    as_text = FrigateReview.model_validate({**base, "start_time": "1763107200.5"})
    as_iso = FrigateReview.model_validate({**base, "start_time": "2025-11-14T08:00:00.5Z"})
    assert as_float.start_time == as_text.start_time == as_iso.start_time == 1763107200.5


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), ("Bob", "Bob"), (["Bob", 0.79], "Bob"), ([], None)],
)
def test_sub_label_shapes(raw: object, expected: str | None) -> None:
    obj = FrigateEventMessage.model_validate(
        {
            "type": "update",
            "after": {
                "id": "e",
                "camera": "c",
                "label": "person",
                "start_time": 1.0,
                "sub_label": raw,
            },
        }
    ).after
    assert obj.sub_label == expected


def test_what_we_cannot_do_without_still_fails() -> None:
    with pytest.raises(ValidationError):
        FrigateReview.model_validate({"id": "r", "start_time": 1.0, "severity": "alert"})


def test_http_review_fixture_parses() -> None:
    review = FrigateReview.model_validate(
        json.loads((FIXTURES / "frigate" / "api" / "review.json").read_text())
    )
    incident = to_incident(review)
    assert isinstance(incident, Incident)
    assert review.has_been_reviewed is False
    assert incident.upstream_summary is not None
