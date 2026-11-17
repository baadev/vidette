"""Events API: list/inspect events, serve their cached media, take feedback.

Events are read straight from the SQLite event store. Since v0.2 (ADR-0009) Vidette
records no footage of its own: a snapshot is whatever the intake cached from the upstream
NVR, and a clip is fetched from Frigate's recordings on first request and cached next to
it. Starring an event only pins the row. Guarded by the `read:events` scope.

Path safety: snapshot/clip files are only served when their resolved path stays under
`storage.media_dir` — rows come from our own DB, but defense in depth is house policy.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError

from vidette.api.errors import problem
from vidette.api.routers.people import EventPeopleOut, event_people
from vidette.auth.deps import require_scope
from vidette.calibration.service import safe_review
from vidette.db import EventRow
from vidette.events.payload import event_clip_url
from vidette.ffmpeg import MediaToolError
from vidette.frigate.client import FrigateError, FrigateNotFoundError
from vidette.linked import find_linked, mosaic
from vidette.runtime import AppRuntime

router = APIRouter(
    prefix="/api/v1",
    tags=["events"],
    dependencies=[Depends(require_scope("read:events"))],
)

# An event still open when its clip is requested gets footage up to "now", capped so a
# stuck-open event cannot demand an hour-long clip from Frigate.
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


class IncidentLinkOut(BaseModel):
    event_id: str
    camera: str
    at: float
    evidence: Literal["face", "look", "time"]  # look: clothes and timing, never a face
    state: str | None = None
    label: str | None = None
    approximate: bool = False


class IncidentOut(BaseModel):
    """`role: alert`: this event alerted and `links` are the other cameras' events;
    `role: linked`: this event is one of those, `links` holds the alert."""

    id: str | None = None
    role: Literal["alert", "linked"]
    reopened: bool = False  # dismissed by the judge, the same person as a critical alert
    links: list[IncidentLinkOut] = Field(default_factory=list)


def event_incident(row: EventRow) -> IncidentOut | None:
    raw = (row.intent or {}).get("incident")
    if not isinstance(raw, dict) or raw.get("role") not in ("alert", "linked"):
        return None
    try:
        if raw["role"] == "alert":
            links = [
                IncidentLinkOut.model_validate(link) for link in raw.get("links") or []
                if isinstance(link, dict)
            ]  # fmt: skip
            return IncidentOut(id=raw.get("id"), role="alert", links=links)
        alert = IncidentLinkOut(
            event_id=str(raw.get("alert")), camera=str(raw.get("alert_camera") or ""),
            at=float(raw.get("alert_at") or row.started_at),
            evidence=raw.get("evidence") or "time", label=raw.get("alert_label"),
            state="confirmed",
        )  # fmt: skip
        return IncidentOut(
            id=raw.get("id"), role="linked", reopened=bool(raw.get("reopened")), links=[alert]
        )
    except (ValidationError, ValueError, TypeError):
        return None


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
    favorite: bool
    snapshot: str | None
    clip: str | None
    source: str | None = None  # "frigate" since v0.2
    upstream: dict[str, Any] | None = None  # what Frigate said: severity, its GenAI summary
    people: EventPeopleOut | None = None  # the local face check, when known people are on
    incident: IncidentOut | None = None  # the other cameras' events tied to it (ADR-0022)


class FeedbackIn(BaseModel):
    verdict: Literal["up", "down"]


class FavoriteIn(BaseModel):
    favorite: bool


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
        favorite=row.favorite,
        snapshot=(
            f"/api/v1/events/{row.id}/snapshot.jpeg" if row.snapshot_path is not None else None
        ),
        clip=event_clip_url(row),
        source=row.source,
        upstream=row.upstream,
        people=event_people(row.id, (row.intent or {}).get("people")),
        incident=event_incident(row),
    )


async def _get_event_or_404(request: Request, event_id: str) -> EventRow:
    row = await _runtime(request).db.get_event(event_id)
    if row is None:
        raise problem(404, "Event not found", _EVENT_GONE)
    return row


def _safe_media_file(runtime: AppRuntime, raw_path: str) -> Path | None:
    """Resolve a DB-supplied path; None unless it is a real file under media_dir or a
    configured clip folder (whose clips are served in place, never copied)."""
    roots = [runtime.config.storage.media_dir.resolve()]
    roots += [folder.path.resolve() for folder in runtime.config.clip_folders]
    path = Path(raw_path).resolve()
    if not any(path.is_relative_to(root) for root in roots) or not path.is_file():
        return None
    return path


_VIDEO_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".mkv": "video/x-matroska"}


@router.get("/events")
async def list_events(
    request: Request,
    camera: str | None = None,
    since_ts: float | None = None,
    favorite: bool = False,
    limit: Annotated[int, Query(ge=1, le=1000)] = 50,
) -> list[EventOut]:
    rows = await _runtime(request).db.list_events(
        camera=camera, since_ts=since_ts, favorite_only=favorite, limit=limit
    )
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
        "this event has no snapshot on disk; snapshots are fetched from the upstream NVR "
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
    """Serve the cached clip, or fetch it from Frigate's recordings once and cache it."""
    runtime = _runtime(request)
    row = await _get_event_or_404(request, event_id)
    if row.clip_path is not None:
        cached = _safe_media_file(runtime, row.clip_path)
        if cached is not None:
            return FileResponse(
                cached, media_type=_VIDEO_TYPES.get(cached.suffix.lower(), "video/mp4")
            )
    client = runtime.frigate_client
    if row.source != "frigate" or client is None:
        raise problem(
            404,
            "Clip not available",
            "this event has no footage Vidette can serve: its clip is gone from the clip "
            "folder, or it did not come from Frigate, or no frigate: section is configured",
        )
    end_ts = (
        row.ended_at
        if row.ended_at is not None
        else min(time.time(), row.started_at + MAX_OPEN_CLIP_SPAN_S)
    )
    media_dir = runtime.config.storage.media_dir
    out_path = media_dir / "clips" / f"{row.id}.mp4"
    try:
        await client.download_clip(row.camera, row.started_at, end_ts, out_path)
    except FrigateNotFoundError as exc:
        raise problem(
            404,
            "Clip not available",
            "Frigate has no recording for this event: enable `record` for the camera in "
            "Frigate, or its retention already removed the footage",
        ) from exc
    except FrigateError as exc:
        raise problem(502, "Frigate unreachable", str(exc)) from exc
    await runtime.db.update_event(row.id, clip_path=str(out_path))
    return FileResponse(out_path, media_type="video/mp4")


@router.post("/events/{event_id}/feedback", status_code=204)
async def event_feedback(event_id: str, body: FeedbackIn, request: Request) -> None:
    """👍/👎 from the web app: the event's feedback and review (when it was given), then
    calibration looks again (ADR-0020), as after an answer in Telegram."""
    runtime = _runtime(request)
    updated = await runtime.db.set_event_feedback(event_id, body.verdict)
    if not updated:
        raise problem(404, "Event not found", _EVENT_GONE)
    row = await runtime.db.get_event(event_id)
    if row is not None:
        intent = dict(row.intent or {})
        answer = "right" if body.verdict == "up" else "wrong"
        review = intent.get("review")
        if not isinstance(review, dict) or review.get("answer") != answer:
            intent["review"] = {"answer": answer, "at": time.time(), "by": "web"}
            await runtime.db.update_event(event_id, intent=intent)
    await safe_review(getattr(runtime, "calibration", None))


@router.post("/events/{event_id}/favorite", status_code=204)
async def event_favorite(event_id: str, body: FavoriteIn, request: Request) -> None:
    """Star/unstar an event. Requires only `read:events` like its neighbors: viewers may
    star what they can see."""
    updated = await _runtime(request).db.set_event_favorite(event_id, body.favorite)
    if not updated:
        raise problem(404, "Event not found", _EVENT_GONE)


# --- the other cameras at the moment of an event (ADR-0017) ---------------------------------


class LinkedClipOut(BaseModel):
    camera: str
    started_at: float
    ended_at: float
    own: bool  # the event's own clip
    approximate: bool  # this camera's video can arrive late: its time is a guess
    same_person: bool  # a face also seen in the event (identity or enrolled person)
    persons: int | None  # how many people the judge counted in that clip, when judged
    scenario: str | None
    event_id: str | None
    clip: str | None  # where to play it


class LinkedOut(BaseModel):
    event_id: str
    moment: float
    start: float
    end: float
    clips: list[LinkedClipOut]
    silent_cameras: list[str]  # configured cameras that recorded nothing then
    mosaic: str | None  # all of them at once, as one video (made on request)


@router.get("/events/{event_id}/linked")
async def event_linked(event_id: str, request: Request) -> LinkedOut:
    """Every clip-folder clip around the event's moment, on every camera."""
    runtime = _runtime(request)
    row = await _get_event_or_404(request, event_id)
    linked = await find_linked(row, runtime.config, runtime.db)
    cameras = [folder.camera for folder in runtime.config.clip_folders]
    clips = [
        LinkedClipOut(
            camera=c.camera, started_at=c.started_at, ended_at=c.ended_at, own=c.own,
            approximate=c.approximate, same_person=c.same_person, persons=c.persons,
            scenario=c.scenario, event_id=c.event_id,
            clip=f"/api/v1/events/{c.event_id}/clip.mp4" if c.event_id else None,
        )
        for c in linked.clips
    ]  # fmt: skip
    many = len(linked.cameras()) >= 2
    return LinkedOut(
        event_id=row.id, moment=linked.window.moment, start=linked.window.start,
        end=linked.window.end, clips=clips, silent_cameras=linked.silent_cameras(cameras),
        mosaic=f"/api/v1/events/{row.id}/linked.mp4" if many else None,
    )  # fmt: skip


@router.get("/events/{event_id}/linked.mp4")
async def event_linked_video(event_id: str, request: Request) -> FileResponse:
    """The cameras at the event's moment as one mosaic video (the first part, when it
    had to be cut to stay under the size limit)."""
    runtime = _runtime(request)
    row = await _get_event_or_404(request, event_id)
    linked = await find_linked(row, runtime.config, runtime.db)
    try:
        made = await mosaic(linked, runtime.config)
    except MediaToolError as exc:
        raise problem(503, "Cannot stitch the clips", str(exc)) from exc
    if made is None:
        raise problem(
            404,
            "Nothing to stitch",
            "no other camera recorded anything around this event's moment (battery cameras "
            "record only while awake)",
        )
    return FileResponse(made.files[0], media_type="video/mp4")
