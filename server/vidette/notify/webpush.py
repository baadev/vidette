"""Web push (VAPID) delivery — PWA notifications with no vendor cloud in between.

The server signs every delivery with its own VAPID keypair (minted once, kept in the
database `meta` table) and talks directly to the push service the *browser* chose when it
subscribed. Design constraints:

- `pywebpush` is synchronous, so each delivery runs in a worker thread — push must never
  block the event loop (recording is sacred);
- push endpoints embed capability secrets, so logs and errors only ever name the
  push-service host, never the full endpoint;
- a subscription the push service reports gone (404/410) is pruned, not retried — browsers
  rotate subscriptions, and a stale row would fail on every event forever.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush  # type: ignore[import-untyped]

from vidette.core.config import ChannelConfig
from vidette.db import Database
from vidette.notify.apprise_channel import _humane_line
from vidette.notify.webhook import NotifyError

logger = logging.getLogger(__name__)

META_PRIVATE_KEY = "vapid_private_pem"
META_PUBLIC_KEY = "vapid_public_key"

_TTL_S = 60
_GONE_STATUSES = (404, 410)  # the push service says this subscription no longer exists


def _public_key_b64url(private_pem: str) -> str:
    """Derive the browser `applicationServerKey` form of the public key: the uncompressed
    P-256 point (65 bytes), base64url-encoded without padding."""
    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise NotifyError(
            f"meta '{META_PRIVATE_KEY}' does not hold an EC private key — delete the "
            f"'{META_PRIVATE_KEY}' and '{META_PUBLIC_KEY}' meta rows to mint a fresh VAPID "
            "pair (browsers will need to resubscribe)"
        )
    point = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return base64.urlsafe_b64encode(point).rstrip(b"=").decode()


async def ensure_vapid_keys(db: Database) -> tuple[str, str]:
    """Return `(private_pem, public_key_b64url)`, minting and persisting the pair on first use.

    Later calls return the stored pair unchanged — the public key is what browsers pass to
    `pushManager.subscribe(...)` as `applicationServerKey`, so it must stay stable for the
    lifetime of every subscription.
    """
    private_pem = await db.get_meta(META_PRIVATE_KEY)
    if private_pem is None:
        generated = ec.generate_private_key(ec.SECP256R1())
        pem = generated.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        await db.set_meta(META_PRIVATE_KEY, pem)
        # Re-read instead of trusting our local copy: if two first calls raced, the row
        # that landed is the single source of truth for both halves of the pair.
        private_pem = await db.get_meta(META_PRIVATE_KEY)
        assert private_pem is not None  # just written; meta rows are never deleted
    public_key = _public_key_b64url(private_pem)
    if await db.get_meta(META_PUBLIC_KEY) != public_key:
        await db.set_meta(META_PUBLIC_KEY, public_key)
    return private_pem, public_key


class WebPushNotifier:
    """Notifier for `kind: webpush` channels. `webpush_fn` is injectable so tests never
    touch a push service; it defaults to `pywebpush.webpush`."""

    kind = "webpush"

    def __init__(
        self,
        db: Database,
        *,
        contact_email: str = "alex@baadev.com",
        webpush_fn: Callable[..., Any] | None = None,
    ) -> None:
        self._db = db
        self._contact_email = contact_email
        self._webpush_fn: Callable[..., Any] = webpush_fn if webpush_fn is not None else webpush

    async def send(self, channel: ChannelConfig, topic: str, payload: dict[str, Any]) -> None:
        subscriptions = await self._db.list_push_subscriptions()
        if not subscriptions:
            return  # nobody subscribed — nothing to deliver, nothing to report
        # Lazy: the first delivery (or GET /api/v1/push/vapid-key) mints the keypair.
        private_pem, _public_key = await ensure_vapid_keys(self._db)

        summary = payload.get("summary")
        media = payload.get("media")
        media = media if isinstance(media, dict) else {}
        data = json.dumps(
            {
                "title": f"Vidette · {payload.get('camera') or 'system'}",
                "body": str(summary) if summary else _humane_line(payload),
                "url": media.get("live_url") or "/#/events",
                "topic": topic,
            },
            separators=(",", ":"),
            default=str,
        )

        delivered = 0
        failed = 0
        for row in subscriptions:
            host = urlsplit(row.endpoint).netloc or "push service"
            try:
                await asyncio.to_thread(
                    self._webpush_fn,
                    subscription_info=row.subscription,
                    data=data,
                    vapid_private_key=private_pem,
                    # A fresh claims dict per delivery: pywebpush caches the audience in it.
                    vapid_claims={"sub": f"mailto:{self._contact_email}"},
                    ttl=_TTL_S,
                )
            except WebPushException as exc:
                status = getattr(exc.response, "status_code", None)
                if status in _GONE_STATUSES:
                    await self._db.delete_push_subscription(row.endpoint)
                    logger.info(
                        "web-push subscription at %s is gone (HTTP %s) — pruned", host, status
                    )
                    continue
                failed += 1
                logger.warning("web-push delivery via %s failed: %s", host, exc)
            except Exception as exc:  # one broken subscription must never sink the rest
                failed += 1
                logger.warning("web-push delivery via %s failed: %s", host, exc)
            else:
                delivered += 1

        if failed and not delivered:
            raise NotifyError(
                f"all {failed} web-push deliveries failed — check the network and VAPID keys"
            )
