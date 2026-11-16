"""Why an alert was wrong: the reasons offered under 👎, most likely first.

A person who taps 👎 ("false alarm") gets a short list of reasons as buttons. The chosen
one is stored on the event (`intent.review`) and in the clip's dataset entry (`reason`,
with the truth label it implies when the policy has that scenario), so the false-alert
analysis can count what actually goes wrong instead of only how often.

The order is a guess from what the event already says: a recording that stalled offers
"the recording froze" first, a recognized face "one of ours", a verdict about a car
"got into a car", one about a person at the house "walked past". Every reason is always
offered; only the order changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from vidette.label.feedback import JUDGES_SCENARIO
from vidette.telegram.i18n import t


@dataclass(frozen=True)
class Reason:
    code: str  # stored on the event and in the dataset
    truth: tuple[str, ...] = ()  # scenarios it stands for, the first the policy knows wins

    @property
    def button(self) -> str:
        """On the button under the alert (`reason.<code>.button` in the catalog)."""
        return t(f"reason.{self.code}.button")

    @property
    def short(self) -> str:
        """On the one button left after the answer ("👎 walked past")."""
        return t(f"reason.{self.code}.short")


REASONS: tuple[Reason, ...] = (
    Reason("passing", ("passer_by",)),
    Reason("car_use", ("car_use", "car_passing")),
    Reason("ours", ("household_member",)),
    Reason("animal", ("animal",)),
    Reason("vehicle", ("car_passing",)),
    Reason("light", ()),
    Reason("stalled", ()),
    Reason("camera", ()),
    Reason("unimportant", (JUDGES_SCENARIO,)),
    Reason("other", ()),
)
BY_CODE: dict[str, Reason] = {reason.code: reason for reason in REASONS}

_CAR_WORDS = ("car", "vehicle")
_PERSON_SCENARIOS = ("person_at_house", "window_peeking", "door_or_gate_attempt")


def _ours(seen: object) -> bool:
    """A face recognized as the household's or marked familiar."""
    if not isinstance(seen, Mapping):
        return False
    identity = seen.get("identity")
    familiar = isinstance(identity, Mapping) and identity.get("status") == "familiar"
    return seen.get("status") == "known" or familiar


def likely(intent: Mapping[str, Any] | None, verdict: Mapping[str, Any]) -> list[Reason]:
    """Every reason, the ones this event most likely needs first."""
    intent = intent or {}
    first: list[str] = []
    stream = intent.get("stream")
    if isinstance(stream, Mapping) and stream.get("interrupted"):
        first.append("stalled")
    people = intent.get("people")
    seen = people.get("seen") if isinstance(people, Mapping) else None
    if isinstance(seen, list) and any(_ours(s) for s in seen):
        first.append("ours")
    label = str(verdict.get("label") or "")
    persons = verdict.get("persons")
    if any(word in label for word in _CAR_WORDS):
        first += ["car_use", "vehicle"]
    if label in _PERSON_SCENARIOS or (isinstance(persons, int) and persons > 0):
        first += ["passing", "ours", "car_use"]
    if persons == 0:
        first += ["light", "animal", "vehicle"]
    first += ["passing", "unimportant"]
    order: list[str] = []
    for code in [*first, *(r.code for r in REASONS)]:
        if code not in order and code in BY_CODE:
            order.append(code)
    order.remove("other")
    order.append("other")  # always last: it asks for a typed answer
    return [BY_CODE[code] for code in order]
