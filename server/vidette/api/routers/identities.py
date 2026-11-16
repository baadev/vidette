"""Anonymous identities from the web app: list, look, name, flag, merge, split, delete.

Faces nobody enrolled, tracked when `people.track_unknown` is on (ADR-0014). Reading
needs `read:config` and every change `write:config`, like the People page: the crops are
biometric data of people who never agreed to anything.

    GET    /api/v1/identities                         state and every identity
    GET    /api/v1/identities/{id}                    one identity: sightings, look-alikes
    PATCH  /api/v1/identities/{id}                    {"name"?, "status"?}
    POST   /api/v1/identities/{id}/merge              {"into": other id}
    POST   /api/v1/identities/{id}/split              {"sightings": [ids]} → a new identity
    POST   /api/v1/identities/{id}/promote            {"person": name} → a known person
    POST   /api/v1/identities/{id}/demote             take a promotion back
    POST   /api/v1/identities/merge                   {"identities": [ids], "into": id}
    POST   /api/v1/identities/sightings/{s}/move      {"into"?, "name"?, "status"?, "person"?}
    DELETE /api/v1/identities/{id}                    every embedding, crop and sighting
    GET    /api/v1/identities/sightings/{s}.jpg       the face crop of one sighting
    GET    /api/v1/identities/sightings/{s}/clip.mp4  its clip (from a clip folder or media)

Nothing here calls out and nothing here emits a system event, so no notification rule
can ever forward a face or an identity.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from vidette.api.errors import problem
from vidette.auth.deps import require_scope
from vidette.auth.service import Principal
from vidette.people.identities import (
    IdentityError,
    IdentityRow,
    IdentityStore,
    SightingRow,
    promote,
)
from vidette.people.store import StoreError
from vidette.runtime import AppRuntime

router = APIRouter(prefix="/api/v1", tags=["identities"])

_READ = require_scope("read:config")
_WRITE = require_scope("write:config")
_HEADERS = {"Cache-Control": "private, no-store"}
Status = Literal["unknown", "suspicious", "familiar", "known"]


class IdentityOut(BaseModel):
    id: int
    label: str  # "Stranger #12", or the name someone gave it
    name: str | None
    status: Status
    person: str | None  # the enrolled person it became
    sightings: int
    days: int
    first_seen: float | None
    last_seen: float | None
    cameras: list[str]
    face: str | None  # URL of the best face crop
    weak: bool = False  # only unclear faces: joined by hand only
    near_known: bool = False  # came close to someone enrolled


class DuplicateOut(BaseModel):
    a: int
    b: int
    score: float
    weak: bool  # the score involves unclear faces: a hint


class IdentitiesOut(BaseModel):
    enabled: bool  # people.enabled and people.track_unknown
    reason: str | None  # why not, and what to do
    dir: str | None
    retention_days: int
    match: float
    identities: list[IdentityOut]
    duplicates: list[DuplicateOut] = Field(default_factory=list)
    warnings: list[str]


class SightingOut(BaseModel):
    id: int
    at: float
    camera: str
    faces: int
    face_px: int
    face: str | None
    clip: str | None  # URL, when the clip is still there
    event: str | None  # the event this sighting belongs to, when there is one
    manual: bool = False  # added by a person
    weak: bool = False  # only unclear faces
    near_known: bool = False  # this sighting came close to someone enrolled


class SimilarOut(BaseModel):
    id: int
    label: str
    score: float
    face: str | None
    weak: bool = False


class IdentityDetailOut(IdentityOut):
    sightings_list: list[SightingOut]
    similar: list[SimilarOut]


class IdentityPatch(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    status: Literal["unknown", "suspicious", "familiar"] | None = None
    clear_name: bool = False


class MergeIn(BaseModel):
    into: int


class SplitIn(BaseModel):
    sightings: list[int] = Field(min_length=1, max_length=10000)


class PromoteIn(BaseModel):
    person: str = Field(min_length=1, max_length=40)


class MergeManyIn(BaseModel):
    identities: list[int] = Field(min_length=1, max_length=500)
    into: int


class MoveIn(BaseModel):
    into: int | None = None  # an existing identity; none: a new one (or `person`)
    name: str | None = Field(default=None, max_length=40)
    status: Literal["unknown", "suspicious", "familiar"] = "unknown"
    person: str | None = Field(default=None, max_length=40)  # a known person instead


class PromoteOut(BaseModel):
    identity: IdentityOut
    person: str
    faces_added: int


def _runtime(request: Request) -> AppRuntime:
    return cast(AppRuntime, request.app.state.runtime)


def _reason(rt: AppRuntime) -> str | None:
    people = rt.config.people
    if not people.enabled:
        return "Off: set people.enabled and people.track_unknown in the config (docs/people.md)."
    if not people.track_unknown:
        return (
            "Off: set people.track_unknown: true in the config and restart. It keeps faces of "
            "people who never agreed to it (docs/people.md#strangers-who-come-back)."
        )
    if rt.identities is None:
        return rt.people_error or "the identities folder is not available; see the server log"
    return None


def _store(rt: AppRuntime) -> IdentityStore:
    if rt.identities is None:
        raise problem(409, "Identities are off", _reason(rt) or "see the server log")
    return rt.identities


def label(row: IdentityRow) -> str:
    return row.name or f"Stranger #{row.id}"


def _face(sighting: int | None) -> str | None:
    return None if sighting is None else f"/api/v1/identities/sightings/{sighting}.jpg"


def _out(row: IdentityRow) -> IdentityOut:
    return IdentityOut(
        id=row.id,
        label=label(row),
        name=row.name,
        status=cast(Status, row.status),
        person=row.person,
        sightings=row.sightings,
        days=row.days,
        first_seen=row.first_seen,
        last_seen=row.last_seen,
        cameras=row.cameras,
        face=_face(row.cover),
        weak=row.weak,
        near_known=row.near_known,
    )


def _safe_clip(rt: AppRuntime, raw: str | None) -> Path | None:
    """A clip is served only from a configured clip folder or the media folder."""
    if not raw:
        return None
    path = Path(raw).resolve()
    roots = [rt.config.storage.media_dir.resolve()]
    roots += [folder.path.resolve() for folder in rt.config.clip_folders]
    if any(path.is_relative_to(root) for root in roots) and path.is_file():
        return path
    return None


async def _sighting_out(rt: AppRuntime, sighting: SightingRow) -> SightingOut:
    event = await rt.db.get_event_by_source(sighting.source, sighting.source_id)
    clip = None
    if _safe_clip(rt, sighting.clip) is not None:
        clip = f"/api/v1/identities/sightings/{sighting.id}/clip.mp4"
    return SightingOut(
        id=sighting.id,
        at=sighting.at,
        camera=sighting.camera,
        faces=sighting.faces,
        face_px=sighting.face_px,
        face=_face(sighting.id) if sighting.has_crop else None,
        clip=clip,
        event=None if event is None else event.id,
        manual=sighting.manual,
        weak=sighting.weak,
        near_known=sighting.known_score >= rt.config.people.identities.near_known,
    )


async def _detail(rt: AppRuntime, store: IdentityStore, identity: int) -> IdentityDetailOut:
    row = await asyncio.to_thread(store.get, identity)
    if row is None:  # a number the merge pass removed: the identity it is now (ADR-0018)
        now = await asyncio.to_thread(store.resolve, identity)
        row = None if now is None else await asyncio.to_thread(store.get, now)
    if row is None:
        raise problem(404, "No such identity", "it may have been merged, split off or deleted")
    identity = row.id
    sightings = await asyncio.to_thread(store.sightings, identity)
    similar = await asyncio.to_thread(store.similar, identity)
    others = {r.id: r for r in await asyncio.to_thread(store.entries)}
    return IdentityDetailOut(
        **_out(row).model_dump(),
        sightings_list=[await _sighting_out(rt, s) for s in sightings],
        similar=[
            SimilarOut(
                id=s.id,
                label=label(others[s.id]),
                score=s.score,
                face=_face(others[s.id].cover),
                weak=s.weak,
            )
            for s in similar
            if s.id in others
        ],
    )


def _fail(exc: Exception) -> HTTPException:
    return problem(422, "Cannot do that", str(exc))


@router.get("/identities")
async def list_identities(
    request: Request, principal: Annotated[Principal, Depends(_READ)]
) -> IdentitiesOut:
    rt = _runtime(request)
    store = rt.identities
    settings = rt.config.people.identities
    rows = [] if store is None else await asyncio.to_thread(store.entries)
    pairs = [] if store is None else await asyncio.to_thread(store.duplicates)
    return IdentitiesOut(
        enabled=store is not None,
        reason=_reason(rt),
        dir=None if store is None else str(store.root),
        retention_days=settings.retention_days,
        match=settings.match,
        identities=[_out(row) for row in rows],
        duplicates=[DuplicateOut(a=d.a, b=d.b, score=d.score, weak=d.weak) for d in pairs],
        warnings=[] if store is None else store.permission_warnings(),
    )


@router.get("/identities/{identity}")
async def get_identity(
    identity: int, request: Request, principal: Annotated[Principal, Depends(_READ)]
) -> IdentityDetailOut:
    rt = _runtime(request)
    return await _detail(rt, _store(rt), identity)


@router.patch("/identities/{identity}")
async def change_identity(
    identity: int,
    body: IdentityPatch,
    request: Request,
    principal: Annotated[Principal, Depends(_WRITE)],
) -> IdentityDetailOut:
    rt = _runtime(request)
    store = _store(rt)
    try:
        if body.clear_name or body.name is not None:
            await asyncio.to_thread(store.rename, identity, None if body.clear_name else body.name)
        if body.status is not None:
            await asyncio.to_thread(store.set_status, identity, body.status)
    except IdentityError as exc:
        raise _fail(exc) from exc
    return await _detail(rt, store, identity)


@router.post("/identities/{identity}/merge")
async def merge_identity(
    identity: int,
    body: MergeIn,
    request: Request,
    principal: Annotated[Principal, Depends(_WRITE)],
) -> IdentityDetailOut:
    rt = _runtime(request)
    store = _store(rt)
    try:
        await asyncio.to_thread(store.merge, identity, body.into)
    except IdentityError as exc:
        raise _fail(exc) from exc
    return await _detail(rt, store, body.into)


@router.post("/identities/{identity}/split")
async def split_identity(
    identity: int,
    body: SplitIn,
    request: Request,
    principal: Annotated[Principal, Depends(_WRITE)],
) -> IdentityDetailOut:
    rt = _runtime(request)
    store = _store(rt)
    try:
        new = await asyncio.to_thread(store.split, identity, body.sightings)
    except IdentityError as exc:
        raise _fail(exc) from exc
    return await _detail(rt, store, new.id)


@router.post("/identities/{identity}/promote")
async def promote_identity(
    identity: int,
    body: PromoteIn,
    request: Request,
    principal: Annotated[Principal, Depends(_WRITE)],
) -> PromoteOut:
    rt = _runtime(request)
    store = _store(rt)
    people = rt.people_store
    if people is None:
        raise problem(503, "People folder unavailable", rt.people_error or "see the logs")
    try:
        person = await asyncio.to_thread(people.create, body.person)
        row, added = await asyncio.to_thread(promote, store, identity, people, person.id)
    except (IdentityError, StoreError) as exc:
        raise _fail(exc) from exc
    return PromoteOut(identity=_out(row), person=person.name, faces_added=added)


@router.post("/identities/{identity}/demote")
async def demote_identity(
    identity: int, request: Request, principal: Annotated[Principal, Depends(_WRITE)]
) -> IdentityDetailOut:
    """Not that person after all: the faces the promotion added are taken back."""
    rt = _runtime(request)
    store = _store(rt)
    try:
        await asyncio.to_thread(store.demote, identity, rt.people_store)
    except IdentityError as exc:
        raise _fail(exc) from exc
    return await _detail(rt, store, identity)


@router.post("/identities/merge")
async def merge_identities(
    body: MergeManyIn, request: Request, principal: Annotated[Principal, Depends(_WRITE)]
) -> IdentityDetailOut:
    """Merge several identities into one (the list's bulk merge)."""
    rt = _runtime(request)
    store = _store(rt)
    try:
        await asyncio.to_thread(store.merge_many, body.identities, body.into)
    except IdentityError as exc:
        raise _fail(exc) from exc
    return await _detail(rt, store, body.into)


@router.post("/identities/sightings/{sighting}/move")
async def move_sighting(
    sighting: int,
    body: MoveIn,
    request: Request,
    principal: Annotated[Principal, Depends(_WRITE)],
) -> IdentityDetailOut:
    """One sighting to another identity, a new one, or a known person (a new identity
    promoted to them, which can be taken back on its page)."""
    rt = _runtime(request)
    store = _store(rt)
    try:
        if body.person is not None:
            people = rt.people_store
            if people is None:
                raise IdentityError("known people are not available here")
            person = await asyncio.to_thread(people.create, body.person)
            target, _created = await asyncio.to_thread(store.move_sighting, sighting, None)
            await asyncio.to_thread(promote, store, target, people, person.id)
        else:
            target, _created = await asyncio.to_thread(
                store.move_sighting, sighting, body.into, name=body.name, status=body.status
            )
    except (IdentityError, StoreError) as exc:
        raise _fail(exc) from exc
    return await _detail(rt, store, target)


@router.delete("/identities/{identity}", status_code=204)
async def delete_identity(
    identity: int, request: Request, principal: Annotated[Principal, Depends(_WRITE)]
) -> None:
    store = _store(_runtime(request))
    try:
        await asyncio.to_thread(store.delete, identity)
    except IdentityError as exc:
        raise problem(404, "No such identity", str(exc)) from exc


@router.get("/identities/sightings/{sighting}.jpg")
async def sighting_face(
    sighting: int, request: Request, principal: Annotated[Principal, Depends(_READ)]
) -> FileResponse:
    store = _store(_runtime(request))
    path = await asyncio.to_thread(store.sighting_crop, sighting)
    if path is None:
        raise problem(404, "No such face", "the sighting is gone or kept no crop")
    return FileResponse(path, media_type="image/jpeg", headers=_HEADERS)


@router.get("/identities/sightings/{sighting}/clip.mp4")
async def sighting_clip(
    sighting: int, request: Request, principal: Annotated[Principal, Depends(_READ)]
) -> FileResponse:
    rt = _runtime(request)
    store = _store(rt)
    row = await asyncio.to_thread(store.sighting, sighting)
    path = None if row is None else _safe_clip(rt, row.clip)
    if path is None:
        raise problem(
            404,
            "No clip",
            "the clip is gone, or it is not in a configured clip folder or the media folder",
        )
    return FileResponse(path, media_type="video/mp4", headers=_HEADERS)
