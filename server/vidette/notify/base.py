"""Notifier protocol — implemented by webhook/webpush/apprise backends in M2.

Contract highlights (docs/events-and-automations.md):
- delivery is queued with retries (3×, exponential backoff + jitter, 10 s timeout);
- per-channel rate limits; failures become system events (the system snitches on itself);
- payloads are rendered from the canonical event shape and filtered by the channel's
  `include` list; webhook bodies are signed (see .signing).
"""

from __future__ import annotations

from typing import Any, Protocol

from vidette.core.config import ChannelConfig


class Notifier(Protocol):
    kind: str

    async def send(self, channel: ChannelConfig, topic: str, payload: dict[str, Any]) -> None:
        """Deliver one rendered notification; raise to trigger the retry policy."""
        ...
