"""Settings the person changes from the web app: which VLM backend judges incidents.

`GET /api/v1/settings/vlm` (read:config) lists the three choices (off, local, cloud) with
the model, the endpoint, where frames go and today's usage against the budget.
`PUT /api/v1/settings/vlm` (write:config) stores the choice in the database; it wins over
`understanding.vlm.backend` in the config until changed again. Choosing a backend that
is not configured (cloud without a `cloud:` section) is refused with what to add. Each
change is recorded as a `vlm.backend_changed` system event.
"""

from __future__ import annotations

from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from vidette.api.errors import problem
from vidette.auth.deps import require_scope
from vidette.auth.service import Principal
from vidette.core.config import VlmBackend
from vidette.runtime import AppRuntime
from vidette.vlm.endpoint import (
    BACKEND_META_KEY,
    available_backends,
    endpoint_for,
    parse_choice,
)

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])

_REQUIRE_READ_CONFIG = require_scope("read:config")
_REQUIRE_WRITE_CONFIG = require_scope("write:config")

_LABELS = {VlmBackend.none: "Off", VlmBackend.local: "Local", VlmBackend.cloud: "Cloud"}


class UsageOut(BaseModel):
    calls: int
    limit: int
    usd: float


class VlmOptionOut(BaseModel):
    id: VlmBackend
    label: str
    available: bool
    reason: str | None = None  # why it cannot be chosen
    model: str | None = None
    endpoint: str | None = None
    privacy: str
    cloud: bool = False
    usage_today: UsageOut | None = None


class WorkerOut(BaseModel):
    running: bool
    policies: int
    judged: int
    confirmed: int
    dismissed: int
    undecided: int
    note: str


class VlmSettingsOut(BaseModel):
    backend: VlmBackend
    source: Literal["ui", "config"]
    config_backend: VlmBackend
    frames_per_call: int
    options: list[VlmOptionOut]
    worker: WorkerOut


class VlmSettingsIn(BaseModel):
    backend: VlmBackend


def _runtime(request: Request) -> AppRuntime:
    return cast(AppRuntime, request.app.state.runtime)


async def _settings(rt: AppRuntime) -> VlmSettingsOut:
    config = rt.config
    stored = parse_choice(await rt.db.get_meta(BACKEND_META_KEY))
    backend = await rt.vlm_backend()
    options: list[VlmOptionOut] = []
    for choice, reason in available_backends(config).items():
        if choice is VlmBackend.none:
            options.append(
                VlmOptionOut(
                    id=choice,
                    label=_LABELS[choice],
                    available=True,
                    privacy="No frames go anywhere. Incidents stay observed and nothing "
                    "notifies.",
                )
            )
            continue
        if reason is not None:
            options.append(
                VlmOptionOut(
                    id=choice,
                    label=_LABELS[choice],
                    available=False,
                    reason=reason,
                    privacy="When configured: frames from your cameras are sent to the "
                    "cloud provider you choose.",
                    cloud=choice is VlmBackend.cloud,
                )
            )
            continue
        endpoint = endpoint_for(config, choice)
        usage = await rt.vlm_ledger.today(choice.value)
        options.append(
            VlmOptionOut(
                id=choice,
                label=_LABELS[choice],
                available=True,
                model=endpoint.model,
                endpoint=endpoint.host,
                privacy=endpoint.privacy,
                cloud=choice is VlmBackend.cloud,
                usage_today=UsageOut(
                    calls=usage.calls, limit=endpoint.max_calls_per_day, usd=round(usage.usd, 4)
                ),
            )
        )
    status = rt.verdicts.status
    policies = len(config.policies)
    if not policies:
        note = "No policies are configured, so nothing is judged."
    elif backend is VlmBackend.none:
        note = "Off: incidents stay observed and nothing notifies."
    else:
        note = (
            "Closed incidents on cameras with a policy are judged; an alert verdict "
            "notifies per your notification rules."
        )
    return VlmSettingsOut(
        backend=backend,
        source="ui" if stored is not None and stored is backend else "config",
        config_backend=config.understanding.vlm.backend,
        frames_per_call=config.understanding.vlm.frames_per_call,
        options=options,
        worker=WorkerOut(
            running=rt.verdicts.running,
            policies=policies,
            judged=status.judged,
            confirmed=status.confirmed,
            dismissed=status.dismissed,
            undecided=status.undecided,
            note=note,
        ),
    )


@router.get("/vlm")
async def get_vlm_settings(
    request: Request, principal: Annotated[Principal, Depends(_REQUIRE_READ_CONFIG)]
) -> VlmSettingsOut:
    return await _settings(_runtime(request))


@router.put("/vlm")
async def put_vlm_settings(
    body: VlmSettingsIn,
    request: Request,
    principal: Annotated[Principal, Depends(_REQUIRE_WRITE_CONFIG)],
) -> VlmSettingsOut:
    rt = _runtime(request)
    reason = available_backends(rt.config)[body.backend]
    if reason is not None:
        raise problem(409, "Backend not configured", reason)
    before = await rt.vlm_backend()
    await rt.db.set_meta(BACKEND_META_KEY, body.backend.value)
    if before is not body.backend:
        await rt.emit(
            "vlm.backend_changed",
            {"from": before.value, "to": body.backend.value, "by": principal.username},
        )
    return await _settings(rt)
