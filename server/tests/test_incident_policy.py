"""The policy with levels the incident and window tests judge under."""

from __future__ import annotations

POLICY = {
    "name": "home",
    "description": "a house",
    "levels": {
        "critical": {
            "wake": "always",
            "scenarios": {
                "door_or_gate_attempt": {"description": "tries a door"},
                "car_tampering": {"description": "tries a car"},
            },
        },
        "elevated": {
            "wake": "unless_known",
            "scenarios": {"person_at_house": {"description": "at the house"}},
        },
        "none": {"wake": "never", "scenarios": {"passer_by": {"description": "walks past"}}},
    },
}
