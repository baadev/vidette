"""Judges that read verdicts a running Vidette already gave, instead of asking a model.

`vidette eval --judge stored-live` scores what the live judge actually said about each
clip of a dataset, as kept in the service's database (`events.intent.verdicts`);
`stored-deep` what deep analysis said (`events.deep.verdicts`, ADR-0019); `stored-best`
the live verdict, or deep analysis' where the live judge gave none. No model is called
and nothing is written: the database is opened read-only.

An entry is matched to its event by the clip's path (a clip folder's events carry it as
`source_ref`). Each judge abstains, with a reason the report counts, when:

- the clip is not in the database (`not in the database`): recorded before the service
  watched the folder, or its event was pruned;
- the event has no verdict for the policy (`not judged`): the live queue never reached
  it (newest first, older than 15 min dropped), deep analysis did not look at it, or the
  judge gave no answer.

An abstention counts as "no alert", which is what happened at home.

The policy is matched by name: a dataset policy made with `--policy-from-config home`
is scored with the verdicts the service gave under its policy `home`. The stored `alert`
is what the service decided then, under the policy as it was that day.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from vidette.db import EventRow, read_events
from vidette.verdicts.base import Case, PolicyText, Verdict

Which = Literal["live", "deep", "best"]
STORED_JUDGES: dict[str, Which] = {
    "stored-live": "live",
    "stored-deep": "deep",
    "stored-best": "best",
}
_DESCRIPTIONS: dict[Which, str] = {
    "live": "what the live judge said then, from the service's database (no model call)",
    "deep": "what deep analysis said, from the service's database (no model call)",
    "best": "the live verdict, or deep analysis' where the live judge gave none",
}


def live_verdicts(row: EventRow) -> list[dict[str, Any]]:
    """The live judge's verdicts on the whole clip. An event whose early window was judged
    without consequence and whose whole clip never was has none: the first seconds are
    not what the service decided on (an early alert that went out is)."""
    intent = row.intent or {}
    if intent.get("phase") == "early" and row.state != "confirmed":
        return []
    return [v for v in intent.get("verdicts") or [] if isinstance(v, dict)]


def stored_verdicts(row: EventRow, which: Which) -> list[dict[str, Any]]:
    """The verdict records kept on an event: the live judge's, deep analysis', or the
    live ones and deep analysis' where the live judge gave none."""
    live = live_verdicts(row)
    deep = [v for v in (row.deep or {}).get("verdicts") or [] if isinstance(v, dict)]
    if which == "live":
        return live
    if which == "deep":
        return deep
    return live if any(not v.get("abstained") for v in live) or not deep else deep


class StoredJudge:
    """A judge whose verdicts are the ones a service stored (see the module docs)."""

    def __init__(self, name: str, which: Which, events: Mapping[str, EventRow]) -> None:
        self._name = name
        self._which = which
        self._events = dict(events)

    @classmethod
    def from_database(cls, name: str, database: Path) -> StoredJudge:
        rows = read_events(database)
        by_clip: dict[str, EventRow] = {}
        for row in rows:
            for path in (row.source_ref, row.clip_path):
                if path:
                    by_clip.setdefault(str(Path(path).resolve()), row)
        return cls(name, STORED_JUDGES[name], by_clip)

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return _DESCRIPTIONS[self._which]

    async def judge(self, case: Case, policy: PolicyText) -> Verdict:
        row = None
        for media in case.media:
            row = self._events.get(str(media.resolve()))
            if row is not None:
                break
        if row is None:
            return Verdict(alert=False, abstained=True, rationale="not in the database")
        record = next(
            (r for r in stored_verdicts(row, self._which) if r.get("policy") == policy.name), None
        )
        if record is None or record.get("abstained"):
            return Verdict(alert=False, abstained=True, rationale="not judged")
        confidence = record.get("confidence")
        latency = record.get("latency_s")
        persons = record.get("persons")
        return Verdict(
            alert=bool(record.get("alert")),
            label=record.get("label") if isinstance(record.get("label"), str) else None,
            level=record.get("level") if isinstance(record.get("level"), str) else None,
            confidence=float(confidence) if isinstance(confidence, int | float) else None,
            rationale=record.get("rationale") if isinstance(record.get("rationale"), str) else None,
            persons=persons if isinstance(persons, int) and persons >= 0 else None,
            latency_s=float(latency) if isinstance(latency, int | float) and latency >= 0 else None,
            model=record.get("model") if isinstance(record.get("model"), str) else None,
        )
