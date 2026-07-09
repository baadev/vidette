"""Event engine (M2): Tier-2 tracks in, confirmed/dismissed events out.

`EventEngine` (engine.py) owns per-camera trackers, applies the promotion rules and the
public-zone suppression from docs/architecture/ai-pipeline.md, persists events and
publishes the canonical payload (docs/events-and-automations.md) on the in-process bus.
`materialize_clip` (clips.py) lazily remuxes an event's footage into a shareable MP4.
"""

from vidette.events.clips import materialize_clip
from vidette.events.engine import EventEngine

__all__ = ["EventEngine", "materialize_clip"]
