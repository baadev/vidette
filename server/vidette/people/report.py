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
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

STRANGER = "stranger"
UNSURE = "unsure"


class SeenPerson(BaseModel):
    """One person in a clip: faces grouped across frames (by name, or by likeness)."""

    n: int = Field(ge=1)  # 1-based within the report; "5.2 Robin" names it on a sheet
    status: Literal["known", "unknown"]
    person: str | None = None  # known: the person's id
    name: str | None = None  # known: the display name
    score: float  # known: the best score for that person; unknown: the best for anyone
    closest: str | None = None  # unknown: the nearest enrolled id (local debugging only)
    frames: list[int] = Field(default_factory=list)  # indexes of the frames checked
    faces: int = 1  # detections grouped here
    face_px: int = 0  # height of the largest face, in the frame's pixels
    crop: str | None = None  # the best aligned crop, relative to the report's media root
    confirmed: str | None = None  # a person id, "stranger" or "unsure", set by a person
    confirmed_at: datetime | None = None


class PeopleReport(BaseModel):
    version: Literal[1] = 1
    checked_at: datetime
    engine: str  # e.g. "yunet-2023mar+sface-2021dec"
    threshold: float  # the global match threshold at check time
    enrolled: int  # how many people were enrolled when this was checked
    frames_checked: int = 0
    frames_with_readable_faces: int = 0
    unclear_frames: int = 0  # frames whose faces were all too small or unclear
    seen: list[SeenPerson] = Field(default_factory=list)

    @property
    def known(self) -> list[SeenPerson]:
        return [person for person in self.seen if person.status == "known"]

    @property
    def unknown(self) -> list[SeenPerson]:
        return [person for person in self.seen if person.status == "unknown"]

    @property
    def frames_without_readable_face(self) -> int:
        return max(0, self.frames_checked - self.frames_with_readable_faces)

    def get(self, n: int) -> SeenPerson | None:
        return next((person for person in self.seen if person.n == n), None)

    def summary(self) -> str:
        """Short, human: "Sam 0.83, 1 unknown, 2 unclear frames" (sheets, CLI, UI)."""
        parts = [f"{p.name or p.person} {p.score:.2f}" for p in self.known]
        if self.unknown:
            parts.append(f"{len(self.unknown)} unknown")
        if self.unclear_frames:
            parts.append(f"{self.unclear_frames} unclear frame(s)")
        if not parts:
            return "no face" if self.frames_checked else "not checked"
        return ", ".join(parts)


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
    if known and names:
        found = "recognized " + ", ".join(f"{p.name or p.person} ({p.score:.2f})" for p in known)
    elif known:
        found = f"recognized {_plural(len(known), 'person')} the household enrolled as known"
    else:
        found = "nobody recognized"
    parts = [
        found,
        f"faces that match nobody enrolled: {len(report.unknown)}",
        f"no clear face in {report.frames_without_readable_face} of "
        f"{report.frames_checked} frames (too small, turned away or absent)",
    ]
    lines = [
        "Local face check (a separate model on this machine, over frames of the whole "
        "clip): " + "; ".join(parts) + ".",
        "An unrecognized or unseen face is not evidence of a stranger; judge people by "
        "what they do.",
    ]
    if quiet and known:
        lines.append(
            "Policy rule: the people recognized above never need an alert by themselves. "
            "If you see anyone else, judge that person by the policy as usual."
        )
    return "\n".join(lines)
