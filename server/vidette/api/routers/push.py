"""Web-push API: the VAPID public key and the subscription lifecycle.

Browser flow: fetch the server's VAPID public key, hand it to
`pushManager.subscribe({applicationServerKey})`, then POST the resulting subscription JSON
here verbatim. Push endpoints embed capability secrets, so subscriptions are stored but
never logged. Delivery lives in `vidette.notify.webpush`, which also prunes subscriptions
the push service reports gone (404/410). Guarded by the `read:events` scope — subscribing
to alerts is exactly the privilege of reading events.
"""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from vidette.api.errors import problem
from vidette.auth.deps import current_principal, require_scope
from vidette.auth.service import Principal
from vidette.notify.webpush import ensure_vapid_keys
from vidette.runtime import AppRuntime

router = APIRouter(
    prefix="/api/v1/push",
    tags=["push"],
    dependencies=[Depends(require_scope("read:events"))],
)


class VapidKeyOut(BaseModel):
    # Uncompressed P-256 point, base64url without padding — feed it to
    # `pushManager.subscribe(...)` as `applicationServerKey`.
    key: str


class SubscriptionRef(BaseModel):
    endpoint: str


def _runtime(request: Request) -> AppRuntime:
    return cast(AppRuntime, request.app.state.runtime)


@router.get("/vapid-key")
async def vapid_key(request: Request) -> VapidKeyOut:
    _private_pem, public_key = await ensure_vapid_keys(_runtime(request).db)
    return VapidKeyOut(key=public_key)


@router.post("/subscriptions", status_code=204)
async def create_subscription(
    body: dict[str, Any],
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
) -> None:
    endpoint = body.get("endpoint")
    keys = body.get("keys")
    if (
        not isinstance(endpoint, str)
        or not endpoint
        or not isinstance(keys, dict)
        or not isinstance(keys.get("p256dh"), str)
        or not isinstance(keys.get("auth"), str)
    ):
        raise problem(
            422,
            "Malformed push subscription",
            "the body must be the browser's PushSubscription JSON — an object with a string "
            "'endpoint' and a 'keys' object carrying 'p256dh' and 'auth'; send "
            "subscription.toJSON() from the pushManager.subscribe(...) result",
        )
    await _runtime(request).db.upsert_push_subscription(endpoint, body, principal.user_id)


@router.delete("/subscriptions", status_code=204)
async def delete_subscription(body: SubscriptionRef, request: Request) -> None:
    deleted = await _runtime(request).db.delete_push_subscription(body.endpoint)
    if not deleted:
        raise problem(
            404,
            "Subscription not found",
            "no push subscription with that endpoint — it may already have been pruned "
            "after the push service reported it gone; re-subscribing via "
            "POST /api/v1/push/subscriptions is safe and idempotent",
        )
