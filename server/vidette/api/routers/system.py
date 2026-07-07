"""System event log (M1): the storage/recorder health feed the UI banner reads.

`GET /api/v1/system/events` returns recent system events (newest first) from the single
SQLite store. Guarded by the `read:streams` scope like the rest of the M1 read surface.
"""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from vidette.auth.deps import require_scope
from vidette.runtime import AppRuntime

router = APIRouter(
    prefix="/api/v1/system",
    tags=["system"],
    dependencies=[Depends(require_scope("read:streams"))],
)


class SystemEventOut(BaseModel):
    at: float
    kind: str
    payload: dict[str, Any]


def _runtime(request: Request) -> AppRuntime:
    return cast(AppRuntime, request.app.state.runtime)


@router.get("/events")
async def system_events(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[SystemEventOut]:
    rows = await _runtime(request).db.recent_system_events(limit=limit)
    return [SystemEventOut(at=row.at, kind=row.kind, payload=row.payload) for row in rows]
