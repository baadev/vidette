"""Events API (M2): list/inspect understood events, serve their media, take feedback.

Events are read straight from the SQLite event store; the snapshot is written at
confirmation time, while the clip is **lazy** — materialized (concat remux via
`events.clips`) on the first request and cached on the row. Guarded by the
`read:events` scope.

Path safety: snapshot/clip files are only served when their resolved path stays under
`storage.media_dir` — rows come from our own DB, but defense in depth is house policy.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from vidette.api.errors import problem
from vidette.auth.deps import require_scope
from vidette.db import EventRow
from vidette.events.clips import materialize_clip
from vidette.runtime import AppRuntime

router = APIRouter(
    prefix="/api/v1",
    tags=["events"],
    dependencies=[Depends(require_scope("read:events"))],
)

# An event still open when its clip is requested gets footage up to "now", capped so a
# stuck-open event cannot demand an hour-long remux.
MAX_OPEN_CLIP_SPAN_S = 300.0

_EVENT_GONE = (
    "no event with that id — list recent ids via GET /api/v1/events "
    "(dismissed events are kept and listed too)"
)


class GeometryOut(BaseModel):
    approach: float | None = None
    dwell_s: float | None = None
    touch: bool = False
    loiter: bool = False
    repeat_pass: int = 0


class EventOut(BaseModel):
    id: str
    camera: str
    started_at: float
    ended_at: float | None
    state: str
    kinds: list[str]
    zones: list[str]
    geometry: GeometryOut
    summary: str | None
    policy: str | None
    feedback: str | None
    snapshot: str | None
    clip: str


class FeedbackIn(BaseModel):
    verdict: Literal["up", "down"]


def _runtime(request: Request) -> AppRuntime:
    return cast(AppRuntime, request.app.state.runtime)


def _event_out(row: EventRow) -> EventOut:
    return EventOut(
        id=row.id,
        camera=row.camera,
        started_at=row.started_at,
        ended_at=row.ended_at,
        state=row.state,
        kinds=row.kinds,
        zones=row.zones,
        geometry=GeometryOut.model_validate(row.geometry),
        summary=row.summary,
        policy=row.policy,
        feedback=row.feedback,
        snapshot=(
            f"/api/v1/events/{row.id}/snapshot.jpeg" if row.snapshot_path is not None else None
        ),
        clip=f"/api/v1/events/{row.id}/clip.mp4",
    )


async def _get_event_or_404(request: Request, event_id: str) -> EventRow:
    row = await _runtime(request).db.get_event(event_id)
    if row is None:
        raise problem(404, "Event not found", _EVENT_GONE)
    return row


def _safe_media_file(runtime: AppRuntime, raw_path: str) -> Path | None:
    """Resolve a DB-supplied path; None unless it is a real file under media_dir."""
    media_root = runtime.config.storage.media_dir.resolve()
    path = Path(raw_path).resolve()
    if not path.is_relative_to(media_root) or not path.is_file():
        return None
    return path


@router.get("/events")
async def list_events(
    request: Request,
    camera: str | None = None,
    since_ts: float | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
) -> list[EventOut]:
    rows = await _runtime(request).db.list_events(camera=camera, since_ts=since_ts, limit=limit)
    return [_event_out(row) for row in rows]


@router.get("/events/{event_id}")
async def get_event(event_id: str, request: Request) -> EventOut:
    return _event_out(await _get_event_or_404(request, event_id))


@router.get("/events/{event_id}/snapshot.jpeg")
async def event_snapshot(event_id: str, request: Request) -> FileResponse:
    row = await _get_event_or_404(request, event_id)
    unavailable = problem(
        404,
        "Snapshot not available",
        "this event has no snapshot on disk — snapshots are best-effort at confirmation "
        "time and may be missing when the stream gateway was unreachable",
    )
    if row.snapshot_path is None:
        raise unavailable
    path = _safe_media_file(_runtime(request), row.snapshot_path)
    if path is None:
        raise unavailable
    return FileResponse(path, media_type="image/jpeg")


@router.get("/events/{event_id}/clip.mp4")
async def event_clip(event_id: str, request: Request) -> FileResponse:
    runtime = _runtime(request)
    row = await _get_event_or_404(request, event_id)

    if row.clip_path is not None:
        cached = _safe_media_file(runtime, row.clip_path)
        if cached is not None:
            return FileResponse(cached, media_type="video/mp4")

    media_dir = runtime.config.storage.media_dir
    out_path = media_dir / row.camera / "events" / row.id / "clip.mp4"
    end_ts = (
        row.ended_at
        if row.ended_at is not None
        else min(time.time(), row.started_at + MAX_OPEN_CLIP_SPAN_S)
    )
    ok = await materialize_clip(runtime.db, media_dir, row.camera, row.started_at, end_ts, out_path)
    if not ok:
        raise problem(
            404,
            "Clip not available",
            "no recorded footage for this event yet — the recorder may still be writing "
            "the segment, or retention already removed it; retry shortly or check "
            "GET /api/v1/recordings for the camera",
        )
    await runtime.db.update_event(row.id, clip_path=str(out_path))
    return FileResponse(out_path, media_type="video/mp4")


@router.post("/events/{event_id}/feedback", status_code=204)
async def event_feedback(event_id: str, body: FeedbackIn, request: Request) -> None:
    updated = await _runtime(request).db.set_event_feedback(event_id, body.verdict)
    if not updated:
        raise problem(404, "Event not found", _EVENT_GONE)
