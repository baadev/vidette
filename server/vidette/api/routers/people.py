"""Known people from the web app: who is enrolled, add photos, confirm faces on events.

Reading needs `read:config`, every change `write:config` (admins by default): enrolled
faces are biometric data, so viewers who may watch events do not see the reference crops.

    GET    /api/v1/people                          state, people, their faces
    POST   /api/v1/people                          {"name"} → a new person (no faces yet)
    DELETE /api/v1/people/{id}                     the person, photos, crops, embeddings
    POST   /api/v1/people/{id}/photos              raw image body (JPEG/PNG/WebP, ≤ 15 MB)
    GET    /api/v1/people/{id}/faces/{face}.jpg    one reference crop
    DELETE /api/v1/people/{id}/faces/{face}        drop one reference
    GET    /api/v1/events/{id}/people/{n}.jpg      the crop of face n seen in an event
    POST   /api/v1/events/{id}/people/{n}          {"answer": name | "stranger" | "unsure"}
    POST   /api/v1/events/{id}/people/{n}/identity {"identity"?, "name"?, "status"?}
    DELETE /api/v1/events/{id}/people/{n}          take the decision about face n back

Every decision about a face can be changed or taken back (ADR-0016): a new answer takes
the previous one back first, exactly (`vidette.people.decisions`).

Nothing here calls out: photos are read in memory, faces found by the local engine and
stored in `people.dir`. No system events are emitted for people changes, so a
notification rule on `system.*` can never forward a name.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError

from vidette.api.errors import problem
from vidette.auth.deps import require_scope
from vidette.auth.service import Principal
from vidette.people.decisions import (
    RELIABLE_FACE_PX,
    FaceContext,
    IdentityChoice,
    decide,
    undo,
)
from vidette.people.identities import IdentityError, SightingContext
from vidette.people.report import IdentityBrief, PeopleReport
from vidette.people.store import NewFace, PeopleStore, Person, StoreError
from vidette.runtime import AppRuntime

router = APIRouter(prefix="/api/v1", tags=["people"])

_READ = require_scope("read:config")
_WRITE = require_scope("write:config")
MAX_PHOTO_BYTES = 15 * 1024 * 1024
_IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
ENROLL_MAX_SIDE = 1280
_HEADERS = {"Cache-Control": "private, no-store"}


class FaceOut(BaseModel):
    id: str
    kind: Literal["enrolled", "confirmed"]
    url: str


class PersonOut(BaseModel):
    id: str
    name: str
    created_at: datetime
    enrolled: int
    confirmed: int
    photos: int
    threshold: float
    rejected: int
    faces: list[FaceOut]


class PeopleOut(BaseModel):
    enabled: bool
    available: bool  # faces can be found here (OpenCV and the models are loaded)
    reason: str | None  # why not, and what to do
    dir: str | None
    threshold: float
    people: list[PersonOut]
    not_enrolled: list[str]
    warnings: list[str]


class PersonIn(BaseModel):
    name: str


class UploadOut(BaseModel):
    faces_found: int
    readable: int
    added: int
    duplicates: int
    unclear: int
    skipped: list[str]  # why a readable face was not added
    person: PersonOut


class AnswerIn(BaseModel):
    answer: str  # a person's name or id, "stranger" or "unsure"


class SeenOut(BaseModel):
    n: int
    status: Literal["known", "unknown", "unclear"]
    person: str | None
    name: str | None
    score: float
    faces: int
    face_px: int
    crop: str | None
    confirmed: str | None
    identity: int | None = None  # the anonymous identity of an unknown face (ADR-0014)
    identity_note: str | None = None  # e.g. "3rd time today, 5 times on 2 days"
    identity_name: str | None = None  # as of the check or the last decision about it
    identity_status: str | None = None
    closest: str | None = None  # an unknown face's nearest enrolled id (a suggestion)
    small: bool = False  # under 64 px or unclear: may not be recognized next time


class EventPeopleOut(BaseModel):
    summary: str
    frames_checked: int
    unclear_frames: int
    seen: list[SeenOut]
    error: str | None = None


class ConfirmOut(BaseModel):
    message: str
    people: EventPeopleOut


class IdentityIn(BaseModel):
    identity: int | None = None  # an existing identity; none: a new one
    name: str | None = Field(default=None, max_length=40)
    status: Literal["unknown", "familiar", "suspicious"] = "unknown"


def _runtime(request: Request) -> AppRuntime:
    return cast(AppRuntime, request.app.state.runtime)


def _store(rt: AppRuntime) -> PeopleStore:
    if not rt.config.people.enabled:
        raise problem(
            409,
            "Known people are off",
            "set people.enabled: true in the config and restart (docs/people.md)",
        )
    if rt.people_store is None:
        raise problem(503, "People folder unavailable", rt.people_error or "see the logs")
    return rt.people_store


def _person_out(store: PeopleStore, person: Person) -> PersonOut:
    view = store.view(person)
    return PersonOut(
        id=view.id,
        name=view.name,
        created_at=view.created_at,
        enrolled=view.enrolled,
        confirmed=view.confirmed,
        photos=view.photos,
        threshold=view.threshold,
        rejected=view.rejected,
        faces=[
            FaceOut(
                id=face.id,
                kind=face.kind,
                url=f"/api/v1/people/{person.id}/faces/{face.id}.jpg",
            )
            for face in store.faces(person.id)
        ],
    )


def event_people(event_id: str, raw: object) -> EventPeopleOut | None:
    """The face check stored with an event, shaped for the web app (None: not checked)."""
    if not isinstance(raw, dict):
        return None
    if "error" in raw and "seen" not in raw:
        return EventPeopleOut(
            summary="face check failed",
            frames_checked=0,
            unclear_frames=0,
            seen=[],
            error=str(raw["error"])[:300],
        )
    try:
        report = PeopleReport.model_validate(raw)
    except ValidationError:
        return None
    summary = report.summary()
    if not report.seen and report.frames_checked:
        summary = "no face found"
    return EventPeopleOut(
        summary=summary,
        frames_checked=report.frames_checked,
        unclear_frames=report.unclear_frames,
        seen=[
            SeenOut(
                n=seen.n,
                status=seen.status,
                person=seen.person,
                name=seen.name,
                score=seen.score,
                faces=seen.faces,
                face_px=seen.face_px,
                crop=(f"/api/v1/events/{event_id}/people/{seen.n}.jpg" if seen.crop else None),
                confirmed=seen.confirmed,
                identity=None if seen.identity is None else seen.identity.id,
                identity_note=None if seen.identity is None else identity_note(seen.identity),
                identity_name=None if seen.identity is None else seen.identity.name,
                identity_status=None if seen.identity is None else seen.identity.status,
                closest=seen.closest if seen.status == "unknown" else None,
                small=seen.status == "unclear" or seen.face_px < RELIABLE_FACE_PX,
            )
            for seen in report.seen
        ],
    )


def identity_note(brief: IdentityBrief) -> str:
    """How often an identity had been seen when this event was checked."""
    if brief.sightings <= 1:
        return "first time"
    today = brief.today()
    note = f"{today} time{'s' if today > 1 else ''} that day"
    if brief.sightings > today:
        note += f", {brief.sightings} on {brief.days} days"
    return note


@router.get("/people")
async def list_people(
    request: Request, principal: Annotated[Principal, Depends(_READ)]
) -> PeopleOut:
    rt = _runtime(request)
    config = rt.config.people
    store = rt.people_store
    reason: str | None
    if not config.enabled:
        reason = "Off: set people.enabled: true in the config and restart (docs/people.md)."
    else:
        reason = rt.people_error
    people = [] if store is None else await asyncio.to_thread(store.people)
    return PeopleOut(
        enabled=config.enabled,
        available=rt.face_engine is not None,
        reason=reason,
        dir=None if store is None else str(store.root),
        threshold=config.threshold,
        people=[_person_out(store, p) for p in people] if store is not None else [],
        not_enrolled=[] if store is None else store.not_enrolled(),
        warnings=[] if store is None else store.permission_warnings(),
    )


@router.post("/people", status_code=201)
async def create_person(
    body: PersonIn, request: Request, principal: Annotated[Principal, Depends(_WRITE)]
) -> PersonOut:
    store = _store(_runtime(request))
    try:
        person = await asyncio.to_thread(store.create, body.name)
    except StoreError as exc:
        raise problem(422, "Cannot add this person", str(exc)) from exc
    return _person_out(store, person)


def _require(store: PeopleStore, person_id: str) -> Person:
    person = store.find(person_id)
    if person is None:
        raise problem(404, "No such person", "list people via GET /api/v1/people")
    return person


@router.delete("/people/{person_id}", status_code=204)
async def delete_person(
    person_id: str, request: Request, principal: Annotated[Principal, Depends(_WRITE)]
) -> None:
    store = _store(_runtime(request))
    person = _require(store, person_id)
    await asyncio.to_thread(store.delete, person.id)


@router.post("/people/{person_id}/photos")
async def add_photo(
    person_id: str, request: Request, principal: Annotated[Principal, Depends(_WRITE)]
) -> UploadOut:
    rt = _runtime(request)
    store = _store(rt)
    person = _require(store, person_id)
    engine = rt.face_engine
    if engine is None:
        raise problem(503, "Face recognition unavailable", rt.people_error or "see the logs")
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in _IMAGE_TYPES:
        raise problem(415, "Not an image", "send the photo as image/jpeg, image/png or image/webp")
    data = await request.body()
    if not data or len(data) > MAX_PHOTO_BYTES:
        raise problem(413, "Photo too large or empty", "send one photo of at most 15 MB")
    try:
        found = await asyncio.to_thread(engine.faces, data, max_side=ENROLL_MAX_SIDE)
    except ValueError as exc:
        raise problem(422, "Unreadable photo", str(exc)) from exc
    readable = [f for f in found if f.readable and f.embedding is not None and f.crop]
    kept = await asyncio.to_thread(store.add_photo, person.id, data, _IMAGE_TYPES[content_type])
    faces = [
        NewFace(f.embedding, f.crop, f.face_px, f"photo:{kept}#{i}")
        for i, f in enumerate(readable, start=1)
        if f.embedding is not None and f.crop is not None
    ]
    result = await asyncio.to_thread(store.add_faces, person.id, faces, kind="enrolled")
    skipped = [
        f"{source}: looks like {other} ({score:.2f})" for source, other, score in result.elsewhere
    ]
    skipped += [f"{source}: unlike the other faces" for source in result.outliers]
    if result.refused_full:
        skipped.append(f"{result.refused_full} over the limit of {store.max_faces} faces")
    return UploadOut(
        faces_found=len(found),
        readable=len(readable),
        added=len(result.added),
        duplicates=result.duplicates,
        unclear=len(found) - len(readable),
        skipped=skipped,
        person=_person_out(store, store.require(person.id)),
    )


@router.get("/people/{person_id}/faces/{face_id}.jpg")
async def person_face(
    person_id: str,
    face_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(_READ)],
) -> FileResponse:
    store = _store(_runtime(request))
    person = _require(store, person_id)
    path = store.face_path(person.id, face_id)
    if path is None:
        raise problem(404, "No such face", "list faces via GET /api/v1/people")
    return FileResponse(path, media_type="image/jpeg", headers=_HEADERS)


@router.delete("/people/{person_id}/faces/{face_id}", status_code=204)
async def delete_face(
    person_id: str,
    face_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(_WRITE)],
) -> None:
    store = _store(_runtime(request))
    person = _require(store, person_id)
    if not await asyncio.to_thread(store.remove_face, person.id, face_id):
        raise problem(404, "No such face", "list faces via GET /api/v1/people")


async def _event_report(rt: AppRuntime, event_id: str) -> tuple[PeopleReport, dict[str, object]]:
    row = await rt.db.get_event(event_id)
    if row is None:
        raise problem(404, "Event not found", "list recent ids via GET /api/v1/events")
    intent = dict(row.intent or {})
    try:
        return PeopleReport.model_validate(intent.get("people")), intent
    except ValidationError as exc:
        raise problem(
            404, "No face check for this event", "faces are checked when people.enabled is on"
        ) from exc


@router.get("/events/{event_id}/people/{n}.jpg")
async def event_face(
    event_id: str, n: int, request: Request, principal: Annotated[Principal, Depends(_READ)]
) -> FileResponse:
    rt = _runtime(request)
    report, _intent = await _event_report(rt, event_id)
    seen = report.get(n)
    root = rt.config.storage.media_dir.resolve()
    if seen is None or seen.crop is None:
        raise problem(404, "No such face", f"this event has {len(report.seen)} face(s)")
    path = (root / seen.crop).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise problem(404, "Face crop gone", "the media folder no longer has this crop")
    return FileResponse(path, media_type="image/jpeg", headers=_HEADERS)


async def _face_context(rt: AppRuntime, event_id: str) -> FaceContext:
    row = await rt.db.get_event(event_id)
    if row is None:
        raise problem(404, "Event not found", "list recent ids via GET /api/v1/events")
    sighting = SightingContext(
        at=row.started_at,
        camera=row.camera,
        source=row.source or "event",
        source_id=row.source_ref or row.id,
        clip=row.clip_path,
    )
    return FaceContext(
        _store(rt),
        rt.face_engine,
        Path(rt.config.storage.media_dir),
        f"event:{event_id}",
        rt.identities,
        sighting,
    )


async def _decided(
    rt: AppRuntime, event_id: str, intent: dict[str, object], report: PeopleReport, message: str
) -> ConfirmOut:
    intent["people"] = report.model_dump(mode="json")
    await rt.db.update_event(event_id, intent=intent)
    out = event_people(event_id, intent["people"])
    assert out is not None
    return ConfirmOut(message=message, people=out)


@router.post("/events/{event_id}/people/{n}")
async def confirm_event_face(
    event_id: str,
    n: int,
    body: AnswerIn,
    request: Request,
    principal: Annotated[Principal, Depends(_WRITE)],
) -> ConfirmOut:
    """This is <person> / a stranger / not sure; replaces an earlier answer."""
    rt = _runtime(request)
    ctx = await _face_context(rt, event_id)
    report, intent = await _event_report(rt, event_id)
    try:
        done = await asyncio.to_thread(decide, report, n, body.answer, ctx)
    except (StoreError, IdentityError) as exc:
        raise problem(422, "Cannot apply this answer", str(exc)) from exc
    return await _decided(rt, event_id, intent, report, done.message)


@router.post("/events/{event_id}/people/{n}/identity")
async def event_face_identity(
    event_id: str,
    n: int,
    body: IdentityIn,
    request: Request,
    principal: Annotated[Principal, Depends(_WRITE)],
) -> ConfirmOut:
    """Put face n in an identity, or in a new one with a name and a disposition."""
    rt = _runtime(request)
    ctx = await _face_context(rt, event_id)
    report, intent = await _event_report(rt, event_id)
    choice = IdentityChoice(body.identity, body.name, body.status)
    try:
        done = await asyncio.to_thread(decide, report, n, choice, ctx)
    except (StoreError, IdentityError) as exc:
        raise problem(422, "Cannot put this face there", str(exc)) from exc
    return await _decided(rt, event_id, intent, report, done.message)


@router.delete("/events/{event_id}/people/{n}")
async def undo_event_face(
    event_id: str, n: int, request: Request, principal: Annotated[Principal, Depends(_WRITE)]
) -> ConfirmOut:
    """Take the decision about face n back, exactly."""
    rt = _runtime(request)
    ctx = await _face_context(rt, event_id)
    report, intent = await _event_report(rt, event_id)
    try:
        done = await asyncio.to_thread(undo, report, n, ctx)
    except (StoreError, IdentityError) as exc:
        raise problem(422, "Cannot take this back", str(exc)) from exc
    return await _decided(rt, event_id, intent, report, done.message)
