"""The other cameras' events an alert is part of (ADR-0022).

An alert is one clip of one camera. The person in it was often on another camera a
minute before; on a test deployment that other clip had been judged ordinary, and the
alert did not say it was the same person. When an alert goes out, the events of
the other cameras within `incident.link_within` of its moment that had a person are
looked at, and each gets the strongest evidence there is:

    face   the same enrolled person or anonymous identity in both (ADR-0012, ADR-0014)
    look   no face to compare, the cameras are neighbours, and the clothes' colours are
           alike (vidette.incident.look): "probably the same person"
    time   only that: someone was there at the same time

Only `face` and `look` count as the same person. Nothing here raises a level, alerts or
suspects anyone: the link is information next to an alert that already went out.

Pure: rows in, links out; the sightings and looks come from callbacks, so tests need no
video and the service decides what is computed and cached.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from vidette.core.config import VidetteConfig
from vidette.db import EventRow
from vidette.incident.look import Look, similarity
from vidette.telegram.routing import alerting_verdict
from vidette.triage.track import Sighting, stored_sightings

Evidence = Literal["face", "look", "time"]
STRONG: frozenset[str] = frozenset({"face", "look"})
_RANK = {"face": 0, "look": 1, "time": 2}

SightingsFor = Callable[[EventRow], Awaitable[list[Sighting]]]
LookFor = Callable[[EventRow, list[Sighting]], Awaitable[Look | None]]


@dataclass(frozen=True)
class Link:
    event_id: str
    camera: str
    started_at: float
    moment: float  # when the person was first seen in it (else its start)
    gap_s: float  # its moment minus the alert's: negative = before the alert
    evidence: Evidence
    score: float | None  # the clothes' similarity, when it was compared
    state: str
    label: str | None
    level: str | None
    rationale: str | None
    approximate: bool  # its camera's video arrives late: its time is a guess

    @property
    def same_person(self) -> bool:
        return self.evidence in STRONG

    def record(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "camera": self.camera,
            "at": round(self.moment, 2),
            "gap_s": round(self.gap_s, 1),
            "evidence": self.evidence,
            "score": self.score,
            "state": self.state,
            "label": self.label,
            "level": self.level,
            "rationale": self.rationale,
            "approximate": self.approximate,
        }


def moment(row: EventRow) -> float:
    """When the event's person was first seen (the judge's or the track's), else its
    start."""
    timeline = (row.intent or {}).get("timeline")
    seen = timeline.get("person_seen") if isinstance(timeline, Mapping) else None
    if isinstance(seen, int | float) and row.started_at <= seen <= row.started_at + 3600:
        return float(seen)
    track = stored_sightings(row.intent)
    if track:
        return row.started_at + track[0].t
    return row.started_at


def face_keys(row: EventRow) -> set[str]:
    """What the face check recognized: enrolled people (`p:<id>`) and anonymous
    identities (`i:<number>`). Read leniently: a report of another version still says
    who it saw."""
    people = (row.intent or {}).get("people")
    seen = people.get("seen") if isinstance(people, Mapping) else None
    keys: set[str] = set()
    for face in seen if isinstance(seen, list) else []:
        if not isinstance(face, Mapping):
            continue
        if face.get("status") == "known" and face.get("person"):
            keys.add(f"p:{face['person']}")
        identity = face.get("identity")
        if isinstance(identity, Mapping) and isinstance(identity.get("id"), int):
            keys.add(f"i:{identity['id']}")
    return keys


def had_person(row: EventRow, threshold: float) -> bool:
    """A person in it by any account: the pre-filter's detector, the judge, faces."""
    intent = row.intent or {}
    triage = intent.get("triage")
    facts = triage.get("facts") if isinstance(triage, Mapping) else None
    person = facts.get("person") if isinstance(facts, Mapping) else None
    if isinstance(person, int | float) and person >= threshold:
        return True
    persons = alerting_verdict(row.intent, row.policy).get("persons")
    if isinstance(persons, int) and persons > 0:
        return True
    return bool(face_keys(row)) or bool(stored_sightings(intent))


def candidates(
    alert: EventRow, rows: list[EventRow], config: VidetteConfig
) -> list[EventRow]:
    """Other cameras' events with a person, within `link_within` of the alert's moment
    (a camera whose video arrives late is searched that much later too)."""
    within = config.incident.seconds("link_within")
    start, end = moment(alert) - within, (alert.ended_at or alert.started_at) + within
    threshold = config.understanding.triage.possible_person
    found = []
    for row in rows:
        if row.id == alert.id or row.camera == alert.camera:
            continue
        lag = config.cross_camera.lag_s(row.camera)
        began, ended = row.started_at - lag, row.ended_at or row.started_at
        if began > end or ended < start:
            continue
        if had_person(row, threshold):
            found.append(row)
    return found


async def find_links(
    alert: EventRow,
    rows: list[EventRow],
    config: VidetteConfig,
    *,
    sightings_for: SightingsFor,
    look_for: LookFor | None,
) -> list[Link]:
    """The links of an alert to the other cameras' events (module docstring), strongest
    evidence first, then by time."""
    settings = config.incident
    mine = face_keys(alert)
    alert_look: Look | None = None
    alert_looked = False
    links: list[Link] = []
    for row in candidates(alert, rows, config):
        evidence: Evidence = "time"
        score: float | None = None
        if mine and mine & face_keys(row):
            evidence = "face"
        elif look_for is not None and settings.look and settings.neighbours_of(
            alert.camera, row.camera
        ):
            if not alert_looked:
                alert_looked = True
                alert_look = await look_for(alert, await sightings_for(alert))
            if alert_look is not None:
                other = await look_for(row, await sightings_for(row))
                if other is not None:
                    score = similarity(alert_look, other)
                    grey = alert_look.grey or other.grey
                    bar = settings.look_threshold_grey if grey else settings.look_threshold
                    if score >= bar:
                        evidence = "look"
        verdict = alerting_verdict(row.intent, row.policy)
        when = moment(row)
        links.append(
            Link(
                event_id=row.id, camera=row.camera, started_at=row.started_at, moment=when,
                gap_s=when - moment(alert), evidence=evidence, score=score, state=row.state,
                label=verdict.get("label"), level=verdict.get("level"),
                rationale=verdict.get("rationale") or row.summary,
                approximate=config.cross_camera.lag_s(row.camera) > 0,
            )
        )  # fmt: skip
    return sorted(links, key=lambda link: (_RANK[link.evidence], link.moment))
