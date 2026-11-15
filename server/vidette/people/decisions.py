"""What a person says about one face, and how to take it back (ADR-0016).

A decision is one answer about face `n` of a clip or an event:

- **this is Sam** (an enrolled person): the face's crop becomes one of Sam's references;
  if it had matched someone else, that person's threshold rises above the wrong score; if
  the face belongs to an anonymous identity, the identity is promoted to Sam (its best
  faces become Sam's references too). A face too unclear to compare teaches nothing:
  the answer is kept, no reference is added;
- **a stranger**: if it had matched someone, that person's threshold rises; the face
  becomes (or stays) an anonymous identity, so it is recognized when it comes back;
- **not sure**: recorded only;
- **this is identity #12**, or **a new identity** with a name and a disposition
  (unknown, familiar, suspicious): the face's sighting moves there, or is added there
  when the face had no identity (a face of any size: a small one may not be recognized
  next time, and the answer says so).

Every change a decision makes is recorded on the face as an `Effect`, so a decision is
taken back exactly (`undo`), and a new decision first takes the previous one back.
Decisions recorded before effects existed are taken back from what they always wrote:
the reference's source `confirmed:<where>#<n>` and the rejection's `where`.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from vidette.people.engine import FaceEngine
from vidette.people.identities import (
    GroupFace,
    IdentityError,
    IdentityStore,
    SightingContext,
    promote,
)
from vidette.people.report import STRANGER, UNSURE, Effect, PeopleReport, SeenPerson
from vidette.people.store import NewFace, PeopleStore, StoreError

IDENTITY = "identity:"
# Faces of 64 px and more matched the same person at a median 0.59 on a test deployment
# (ADR-0014); smaller ones often fall below the 0.5 an identity needs.
RELIABLE_FACE_PX = 64
SAME_CLIP_FACE = 0.45  # a face this close to a sighting of the same clip is that sighting
SMALL_NOTE = (
    "a small face ({px} px): it may not be recognized next time; a clearer face of the same "
    "person, added the same way, helps"
)


@dataclass(frozen=True)
class FaceContext:
    """Everything a decision may touch, and where the face was seen."""

    store: PeopleStore
    engine: FaceEngine | None
    media_root: Path  # the report's crops are relative to this
    where: str  # "event:<id>" or "clip:<id>"
    identities: IdentityStore | None = None
    sighting: SightingContext | None = None  # the event or clip, for identity sightings


@dataclass(frozen=True)
class IdentityChoice:
    """Put the face in identity `identity`, or in a new one with `name` and `status`."""

    identity: int | None = None
    name: str | None = None
    status: str = "unknown"


@dataclass(frozen=True)
class Confirmation:
    seen: SeenPerson
    answer: str  # a person id, "stranger", "unsure", "identity:<id>" or "" (taken back)
    message: str


def _crop(seen: SeenPerson, n: int, ctx: FaceContext) -> bytes:
    if seen.crop is None:
        raise StoreError(f"face {n} has no crop to learn from")
    path = (ctx.media_root / seen.crop).resolve()
    if not path.is_relative_to(ctx.media_root.resolve()) or not path.is_file():
        raise StoreError(f"the crop for face {n} is gone ({seen.crop})")
    return path.read_bytes()


def _engine(ctx: FaceContext) -> FaceEngine:
    if ctx.engine is None:
        raise StoreError("face recognition is not available here (models or OpenCV)")
    return ctx.engine


def _identities(ctx: FaceContext) -> tuple[IdentityStore, SightingContext]:
    if ctx.identities is None or ctx.sighting is None:
        raise StoreError(
            "identities are off here (people.track_unknown) or this face has no event to "
            "attach a sighting to"
        )
    return ctx.identities, ctx.sighting


def _get(report: PeopleReport, n: int) -> SeenPerson:
    seen = report.get(n)
    if seen is None:
        raise StoreError(f"there is no face {n} here (faces: {len(report.seen)})")
    return seen


def _refresh_identity(seen: SeenPerson, ctx: FaceContext, identity: int, sighting: int) -> None:
    if ctx.identities is None:
        return
    at = ctx.sighting.at if ctx.sighting is not None else None
    try:
        seen.identity = ctx.identities.brief(identity, at, sighting=sighting)
    except IdentityError:
        seen.identity = None


def _undo(seen: SeenPerson, n: int, ctx: FaceContext) -> list[str]:
    """Take back everything the face's current decision changed; what was undone."""
    done: list[str] = []
    store, ids = ctx.store, ctx.identities
    if seen.confirmed and not seen.effects:  # recorded before effects existed
        for person in store.people():
            for face in store.faces(person.id):
                if face.source == f"confirmed:{ctx.where}#{n}":
                    store.remove_face(person.id, face.id)
                    done.append(f"{person.name}'s reference")
        if seen.person is not None and store.unreject(seen.person, ctx.where):
            done.append(f"{seen.name or seen.person}'s threshold")
    for effect in reversed(seen.effects):
        with contextlib.suppress(StoreError, IdentityError):
            if effect.kind == "face" and effect.person and effect.face:
                if store.remove_face(effect.person, effect.face):
                    done.append("the reference face")
            elif effect.kind == "reject" and effect.person and effect.where:
                if store.unreject(effect.person, effect.where, effect.score):
                    done.append("the stricter threshold")
            elif effect.kind == "promote" and ids and effect.identity:
                ids.demote(effect.identity, store)
                done.append(f"#{effect.identity} as a known person")
            elif effect.kind == "status" and ids and effect.identity and effect.old_status:
                ids.set_status(effect.identity, effect.old_status)
            elif effect.kind == "sighting" and ids and effect.sighting:
                ids.delete_sighting(effect.sighting)
                if effect.created and effect.identity:
                    ids.delete_if_empty(effect.identity)
                done.append("the sighting")
                seen.identity = None
            elif effect.kind == "move" and ids and effect.sighting and effect.previous:
                ids.move_sighting(effect.sighting, effect.previous)
                if effect.created and effect.identity:
                    ids.delete_if_empty(effect.identity)
                _refresh_identity(seen, ctx, effect.previous, effect.sighting)
                done.append(f"the move to #{effect.identity}")
    seen.effects = []
    seen.confirmed = None
    seen.confirmed_at = None
    return done


def undo(report: PeopleReport, n: int, ctx: FaceContext) -> Confirmation:
    """Take back the decision about face `n` (the caller saves the report)."""
    seen = _get(report, n)
    if not seen.confirmed:
        return Confirmation(seen, "", "nothing to take back")
    done = _undo(seen, n, ctx)
    message = "taken back" + (f": {', '.join(done)}" if done else "")
    return Confirmation(seen, "", message)


def _to_identity(
    seen: SeenPerson,
    n: int,
    ctx: FaceContext,
    choice: IdentityChoice,
    effects: list[Effect],
) -> tuple[int, str]:
    """Put the face's sighting in the chosen identity; (identity, message)."""
    ids, where = _identities(ctx)
    current = seen.identity.sighting if seen.identity is not None else None
    crop: bytes | None = None
    embedding = None
    if current is None:  # checked before it was tracked: the backfill may have it already
        crop = _crop(seen, n, ctx)
        embedding = _engine(ctx).embed_crop(crop)
        current = ids.same_face(where.source, where.source_id, embedding, SAME_CLIP_FACE)
    if current is not None and ids.sighting(current) is not None:
        previous = ids.sighting(current)
        assert previous is not None
        if choice.identity is not None and previous.identity == choice.identity:
            return choice.identity, f"already #{choice.identity}"
        identity, created = ids.move_sighting(
            current, choice.identity, name=choice.name, status=choice.status
        )
        effects.append(
            Effect(kind="move", identity=identity, sighting=current,
                   previous=previous.identity, created=created)
        )  # fmt: skip
        _refresh_identity(seen, ctx, identity, current)
        return identity, f"moved to #{identity}"
    crop = crop if crop is not None else _crop(seen, n, ctx)
    embedding = embedding if embedding is not None else _engine(ctx).embed_crop(crop)
    weak = seen.status == "unclear" or seen.face_px < ids.settings.min_face_px
    small = weak or seen.face_px < RELIABLE_FACE_PX
    identity, sighting, created = ids.add_manual(
        where,
        GroupFace(embedding, seen.face_px, 1.0, crop, quality=0.0),
        into=choice.identity,
        name=choice.name,
        status=choice.status,
        weak=weak,
        known_score=seen.score if seen.status == "unknown" else 0.0,
    )
    effects.append(Effect(kind="sighting", identity=identity, sighting=sighting, created=created))
    _refresh_identity(seen, ctx, identity, sighting)
    message = f"{'new identity' if created else 'added to'} #{identity}"
    if small:
        message += "; " + SMALL_NOTE.format(px=seen.face_px)
    return identity, message


def decide(
    report: PeopleReport, n: int, answer: str | IdentityChoice, ctx: FaceContext
) -> Confirmation:
    """Apply one decision to face `n`, taking the previous one back first, and mark it in
    `report` (the caller saves the report). `answer`: a person's name or id, "stranger",
    "unsure", or an `IdentityChoice`."""
    seen = _get(report, n)
    store = ctx.store
    person = None
    if isinstance(answer, str) and answer.strip().lower() not in (STRANGER, UNSURE):
        person = store.require(answer)  # an unknown name fails before anything changes
    if isinstance(answer, IdentityChoice):
        _identities(ctx)
    if seen.confirmed:
        _undo(seen, n, ctx)
    effects: list[Effect] = []
    message: str
    if isinstance(answer, IdentityChoice):
        if seen.status == "known" and seen.person is not None:
            store.reject(seen.person, seen.score, ctx.where)
            effects.append(
                Effect(kind="reject", person=seen.person, where=ctx.where, score=seen.score)
            )
        identity, message = _to_identity(seen, n, ctx, answer, effects)
        result = f"{IDENTITY}{identity}"
    elif answer.strip().lower() == UNSURE:
        result, message = UNSURE, "noted as unsure; nothing learned"
    elif answer.strip().lower() == STRANGER:
        result = STRANGER
        parts = []
        if seen.status == "known" and seen.person is not None:
            raised = store.reject(seen.person, seen.score, ctx.where)
            effects.append(
                Effect(kind="reject", person=seen.person, where=ctx.where, score=seen.score)
            )
            parts.append(f"not {seen.name}; {seen.name}'s threshold is now {raised:.2f}")
        if ctx.identities is not None and ctx.sighting is not None:
            if seen.identity is None:
                try:
                    _identity, note = _to_identity(seen, n, ctx, IdentityChoice(), effects)
                except (StoreError, IdentityError) as exc:
                    note = f"no identity: {exc}"
                parts.append(note)
            else:
                parts.append(f"it is #{seen.identity.id}")
        else:
            parts.append("nothing about this face is kept in the people folder")
        message = "noted as a stranger; " + "; ".join(parts)
    else:
        assert person is not None
        result = person.id
        message = _to_person(seen, n, ctx, person.id, person.name, effects)
    seen.confirmed = result
    seen.confirmed_at = datetime.now().astimezone().replace(microsecond=0)
    seen.effects = effects
    return Confirmation(seen=seen, answer=result, message=message)


def _to_person(
    seen: SeenPerson, n: int, ctx: FaceContext, pid: str, name: str, effects: list[Effect]
) -> str:
    store = ctx.store
    message = ""
    if seen.status == "unclear":
        message = f"noted as {name}; the face is too unclear to learn from"
    else:
        crop = _crop(seen, n, ctx)
        added = store.add_faces(
            pid,
            [NewFace(_engine(ctx).embed_crop(crop), crop, seen.face_px,
                     f"confirmed:{ctx.where}#{n}")],
            kind="confirmed",
            check_outliers=False,
        )  # fmt: skip
        effects += [Effect(kind="face", person=pid, face=face) for face in added.added]
        if added.added:
            message = f"added to {name}'s references"
        elif added.duplicates:
            message = f"{name} already has this face; nothing added"
        elif added.elsewhere:
            _source, other, score = added.elsewhere[0]
            message = f"not added: it looks more like {other} ({score:.2f})"
        else:
            message = f"not added: {name} has {store.max_faces} faces already"
    if seen.status == "known" and seen.person not in (None, pid):
        assert seen.person is not None
        store.reject(seen.person, seen.score, ctx.where)
        effects.append(Effect(kind="reject", person=seen.person, where=ctx.where, score=seen.score))
    if ctx.identities is not None and seen.identity is not None:
        try:
            _row, more = promote(ctx.identities, seen.identity.id, store, pid)
        except (IdentityError, StoreError):
            pass  # deleted or merged since: the face itself is still learned
        else:
            effects.append(Effect(kind="promote", identity=seen.identity.id, person=pid))
            message += f"; stranger #{seen.identity.id} is {name} now"
            if more:
                message += f" ({more} more face(s) from earlier clips)"
    return message


def confirm(
    report: PeopleReport,
    n: int,
    answer: str,
    *,
    store: PeopleStore,
    engine: FaceEngine | None,
    media_root: Path,
    where: str,
    identities: IdentityStore | None = None,
    sighting: SightingContext | None = None,
) -> Confirmation:
    """`decide` with a person, "stranger" or "unsure" (the older entry point: the CLI,
    contact-sheet replies, the bot and the web app)."""
    ctx = FaceContext(store, engine, media_root, where, identities, sighting)
    return decide(report, n, answer, ctx)
