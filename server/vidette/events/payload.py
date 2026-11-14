"""The canonical event payload shared by the WebSocket feed, webhooks and MQTT
(docs/events-and-automations.md), built from a stored event row."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from vidette.db import EventRow


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat().replace("+00:00", "Z")


def canonical_payload(row: EventRow, topic: str) -> dict[str, Any]:
    geometry = row.geometry or {}
    return {
        "event": topic,
        "id": row.id,
        "state": row.state,
        "camera": row.camera,
        "started_at": _iso_utc(row.started_at),
        "ended_at": _iso_utc(row.ended_at) if row.ended_at is not None else None,
        "kinds": list(row.kinds),
        "zones": list(row.zones),
        "geometry": {
            "approach": geometry.get("approach"),
            "dwell_s": geometry.get("dwell_s"),
            "touch": bool(geometry.get("touch", False)),
            "loiter": bool(geometry.get("loiter", False)),
            "repeat_pass": int(geometry.get("repeat_pass", 0)),
        },
        "summary": row.summary,  # Vidette's own verdict text (week 2); None until then
        "intent": row.intent,
        "policy": row.policy,
        "source": row.source,
        "upstream": row.upstream,  # what Frigate said: severity, its own GenAI summary
        "media": {
            "snapshot": f"/api/v1/events/{row.id}/snapshot.jpeg" if row.snapshot_path else None,
            "clip": event_clip_url(row),
        },
    }


def event_clip_url(row: EventRow) -> str | None:
    """Clips exist for events Vidette can fetch footage for: cached, or from Frigate."""
    if row.clip_path is not None or row.source == "frigate":
        return f"/api/v1/events/{row.id}/clip.mp4"
    return None
