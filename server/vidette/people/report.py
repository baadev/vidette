"""What a face check found in one clip or event, and the line the judge is told.

The report is stored next to the clip (`clips/<id>.json`, key `people`) or in the event's
verdict record, and read by the contact sheets and the web app. It holds names and scores,
paths to aligned face crops and the person's later confirmations; never embeddings: a
face that matched nobody is not kept as a biometric template anywhere.

Three kinds of face, so "not recognized" is never confused with "a stranger":

- **known**: readable and at or above the person's threshold;
- **unknown**: readable (big and clear enough) but matching nobody enrolled;
- **unclear**: a face was found but is too small, blurred or turned away to tell. Such
  faces are only counted per frame; they say nothing about who it is.

With `people.track_unknown` (ADR-0014) an unknown face that is big and clear enough is
also matched against the anonymous identities seen before, and the seen person carries an
`IdentityBrief`: the identity's number, and when and where it was seen. That is counts
and camera names only, never an embedding or a crop.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

STRANGER = "stranger"
UNSURE = "unsure"
IdentityStatus = Literal["unknown", "suspicious", "familiar", "known"]
FACTS_WINDOW_S = 3600.0  # "cameras in the last hour" in the judge's line


class IdentityBrief(BaseModel):
    """The anonymous identity an unknown face was matched to (ADR-0014), as of this
    sighting: its number, its status and when and where it was seen. Counts and camera
    names only, so it can be stored with an event and counted for any judge; the local
    name someone gave it goes to a local judge only, like the names of known people."""

    id: int = Field(ge=1)  # shown as "stranger #12"; never reused
    name: str | None = None  # given by someone in the web app, optional
    status: IdentityStatus = "unknown"
    new: bool = False  # this sighting created the identity
    at: float  # this sighting, unix seconds
    sightings: int = Field(default=1, ge=1)  # in total, this one included
    days: int = Field(default=1, ge=1)  # distinct local days with a sighting
    first_seen: float
    # (time, camera) of the sightings in the week up to this one, newest first, this one
    # included: what recurrence rules count (vidette.people.recurrence)
    recent: list[tuple[float, str]] = Field(default_factory=list)
    sighting: int | None = None  # this sighting's id in the identity store
    weak: bool = False  # no face of it clear enough to compare: joined only by hand
    near_known: bool = False  # close to someone enrolled: probably them, never raises

    def within(self, seconds: float) -> list[tuple[float, str]]:
        """Sightings from `seconds` before this one up to it (this one included)."""
        return [(at, cam) for at, cam in self.recent if self.at - seconds <= at <= self.at]

    def cameras_within(self, seconds: float) -> list[str]:
        """Distinct cameras in that window, oldest sighting first."""
        cameras: list[str] = []
        for _at, camera in sorted(self.within(seconds)):
            if camera not in cameras:
                cameras.append(camera)
        return cameras

    def today(self) -> int:
        """Sightings on this sighting's local date, up to it (this one included)."""
        day = datetime.fromtimestamp(self.at).date()
        return max(
            1,
            sum(
                1
                for at, _cam in self.recent
                if at <= self.at and datetime.fromtimestamp(at).date() == day
            ),
        )


class Effect(BaseModel):
    """One change a decision about a face made, kept so the decision can be taken back
    exactly (vidette.people.decisions, ADR-0016)."""

    kind: Literal["face", "reject", "promote", "sighting", "move", "status"]
    person: str | None = None  # face, reject, promote: the enrolled person
    face: str | None = None  # face: the reference added to them
    where: str | None = None  # reject: the clip or event it came from
    score: float | None = None  # reject: the wrong match's score
    identity: int | None = None  # promote, sighting, move, status: the identity
    sighting: int | None = None  # sighting, move: the sighting
    previous: int | None = None  # move: the identity it came from
    created: bool = False  # sighting, move: the identity was created by the decision
    old_status: str | None = None  # status: what it was before


class SeenPerson(BaseModel):
    """One person in a clip: faces grouped across frames (by name, by likeness, or, for
    faces too small to compare, by where they are from one frame to the next)."""

    n: int = Field(ge=1)  # 1-based within the report; "5.2 Robin" names it on a sheet
    # known: matched someone enrolled; unknown: readable, matched nobody; unclear: a face
    # was followed through frames but never clear enough to compare (ADR-0016)
    status: Literal["known", "unknown", "unclear"]
    person: str | None = None  # known: the person's id
    name: str | None = None  # known: the display name
    score: float  # known: the best score for that person; unknown: the best for anyone
    closest: str | None = None  # unknown: the nearest enrolled id (local debugging only)
    frames: list[int] = Field(default_factory=list)  # indexes of the frames checked
    faces: int = 1  # detections grouped here
    face_px: int = 0  # height of the largest face, in the frame's pixels
    crop: str | None = None  # the best aligned crop, relative to the report's media root
    # a person's decision: a person id, "stranger", "unsure" or "identity:<id>"
    confirmed: str | None = None
    confirmed_at: datetime | None = None
    effects: list[Effect] = Field(default_factory=list)  # what the decision changed
    identity: IdentityBrief | None = None  # people.track_unknown (ADR-0014, ADR-0016)
    best_at_s: float | None = None  # when in the clip the best picture of this face is


class PeopleReport(BaseModel):
    version: Literal[1] = 1
    checked_at: datetime
    engine: str  # e.g. "yunet-2023mar+sface-2021dec"
    threshold: float  # the global match threshold at check time
    enrolled: int  # how many people were enrolled when this was checked
    frames_checked: int = 0
    frames_with_readable_faces: int = 0
    unclear_frames: int = 0  # frames whose faces were all too small or unclear
    # frames looked at again around the faces, for the best moment (ADR-0016); not part
    # of the counts above, which stay comparable with checks before it
    extra_frames: int = 0
    seen: list[SeenPerson] = Field(default_factory=list)

    @property
    def known(self) -> list[SeenPerson]:
        return [person for person in self.seen if person.status == "known"]

    @property
    def unknown(self) -> list[SeenPerson]:
        return [person for person in self.seen if person.status == "unknown"]

    @property
    def unclear(self) -> list[SeenPerson]:
        return [person for person in self.seen if person.status == "unclear"]

    @property
    def familiar(self) -> list[SeenPerson]:
        """Faces of identities the household marked as familiar (a neighbour): not
        enrolled, not strangers either; treated like known people when routing."""
        return [p for p in self.seen if p.status != "known" and is_familiar(p)]

    @property
    def strangers(self) -> list[SeenPerson]:
        """Readable faces that are neither enrolled nor familiar."""
        return [p for p in self.unknown if not is_familiar(p)]

    @property
    def frames_without_readable_face(self) -> int:
        return max(0, self.frames_checked - self.frames_with_readable_faces)

    def get(self, n: int) -> SeenPerson | None:
        return next((person for person in self.seen if person.n == n), None)

    def summary(self) -> str:
        """Short, human: "Sam 0.83, 1 unknown, 2 unclear frames" (sheets, CLI, UI)."""
        parts = [f"{p.name or p.person} {p.score:.2f}" for p in self.known]
        parts += [f"familiar #{p.identity.id}" for p in self.familiar if p.identity]
        if self.strangers:
            parts.append(f"{len(self.strangers)} unknown")
        unclear = [p for p in self.unclear if not is_familiar(p)]
        if unclear:
            parts.append(f"{len(unclear)} small face(s)")
        if self.unclear_frames:
            parts.append(f"{self.unclear_frames} unclear frame(s)")
        if not parts:
            return "no face" if self.frames_checked else "not checked"
        return ", ".join(parts)


def is_familiar(seen: SeenPerson) -> bool:
    return seen.identity is not None and seen.identity.status == "familiar"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def people_line(report: PeopleReport | None, *, names: bool, quiet: bool = False) -> str:
    """The facts a VLM is told about faces, or "" when there is nothing to say.

    `names=False` (a cloud model) replaces names with "a known person". The model never
    learns who is enrolled, only who was recognized here. With `quiet` (the policy's
    `known_people: quiet`), it is told that known people do not need an alert.
    """
    if report is None or report.frames_checked == 0:
        return ""
    known = report.known
    familiar = report.familiar
    if known and names:
        found = "recognized " + ", ".join(f"{p.name or p.person} ({p.score:.2f})" for p in known)
    elif known:
        found = f"recognized {_plural(len(known), 'person')} the household enrolled as known"
    else:
        found = "nobody recognized"
    if familiar:
        named = [p.identity.name for p in familiar if p.identity and p.identity.name]
        if names and named:
            found += "; familiar to the household (not enrolled, never a concern by themselves): "
            found += ", ".join(named)
        else:
            found += (
                f"; {_plural(len(familiar), 'person')} the household marked as familiar "
                "(a neighbour or a regular visitor)"
            )
    parts = [
        found,
        f"faces that match nobody enrolled: {len(report.strangers)}",
        f"no clear face in {report.frames_without_readable_face} of "
        f"{report.frames_checked} frames (too small, turned away or absent)",
    ]
    lines = [
        "Local face check (a separate model on this machine, over frames of the whole "
        "clip): " + "; ".join(parts) + ".",
        "An unrecognized or unseen face is not evidence of a stranger; judge people by "
        "what they do.",
    ]
    if quiet and (known or familiar):
        lines.append(
            "Policy rule: the people recognized above (known or familiar) never need an alert "
            "by themselves. If you see anyone else, judge that person by the policy as usual."
        )
    lines += recurrence_lines(report, names=names)
    return "\n".join(lines)


_SUFFIXES = {1: "st", 2: "nd", 3: "rd"}


def _ordinal(count: int) -> str:
    suffix = "th" if 10 <= count % 100 <= 20 else _SUFFIXES.get(count % 10, "th")
    return f"{count}{suffix}"


def recurrence_lines(report: PeopleReport, *, names: bool) -> list[str]:
    """What the anonymous identities say about the unknown faces (ADR-0014): behaviour
    over time only, as counts. `names=False` (a cloud model) leaves out names someone
    gave an identity; the numbers are local and mean nothing elsewhere."""
    facts: list[str] = []
    for seen in report.unknown:
        brief = seen.identity
        if brief is None or brief.status in ("known", "familiar"):
            continue  # familiar people are reported above, with the known ones
        who = f"unknown face #{brief.id}"
        if names and brief.name:
            who += f" (the household calls them {brief.name!r})"
        if brief.sightings <= 1:
            text = f"{who}: not seen before by this system"
        else:
            today = brief.today()
            text = f"{who}: this is the {_ordinal(today)} time today"
            if brief.sightings > today:
                text += (
                    f", {_plural(brief.sightings, 'time')} in total on {_plural(brief.days, 'day')}"
                )
            cameras = brief.cameras_within(FACTS_WINDOW_S)
            if len(cameras) > 1:
                text += f"; cameras in the last hour: {', '.join(cameras)}"
        if brief.status == "suspicious":
            text += "; the household marked this person as suspicious"
        if brief.near_known:
            text += "; resembles someone enrolled, possibly a household member"
        facts.append(text + ".")
    if not facts:
        return []
    return [
        "Unknown faces compared with earlier clips on this machine (anonymous, by face only): "
        + " ".join(facts),
        "Someone who keeps coming back, or looks at one place and then another, deserves more "
        "attention than a single pass; that alone does not prove intent.",
    ]
