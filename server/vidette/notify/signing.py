"""Webhook payload signing — the reference implementation of the contract in
docs/events-and-automations.md#verifying-signatures.

Scheme: X-Vidette-Signature: sha256=HEX( HMAC-SHA256(secret, "{timestamp}." + body) ),
with X-Vidette-Timestamp carrying unix seconds; receivers reject stale timestamps to
prevent replay.
"""

from __future__ import annotations

import hashlib
import hmac
import time

SIGNATURE_PREFIX = "sha256="
DEFAULT_MAX_AGE_S = 300


def sign(secret: str, timestamp: int, body: bytes) -> str:
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return SIGNATURE_PREFIX + digest.hexdigest()


def verify(
    secret: str,
    timestamp: int,
    body: bytes,
    signature: str,
    *,
    max_age_s: int = DEFAULT_MAX_AGE_S,
    now: int | None = None,
) -> bool:
    """Constant-time verification + freshness check. Returns False, never raises."""
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > max_age_s:
        return False
    expected = sign(secret, timestamp, body)
    return hmac.compare_digest(expected, signature)
