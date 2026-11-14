"""Reviewing drafts by number: "3 courier, 7 alert, rest ok" → label changes.

The reply is written on a phone, in Russian or English, so the parser is forgiving about
form and strict about meaning:

- `N ok` (верно, да, +) accepts the draft as it is;
- `N <scenario>` sets the scenario, by its id or an alias (`courier`, `курьер`,
  `кот`, `фары`, … built in for the starter vocabulary; more under `aliases:`);
- `N alert` (тревога) / `N silent` (тихо): accepted when the draft already says so;
  otherwise the scenario is set if exactly one fits, and the reply is refused with the
  candidates if several do (truth is a scenario, never a bare alert flag);
- `N ?` (skip, не знаю) leaves the entry unlabelled, marked as reviewed;
- `rest ok` (остальные ок, все ок) accepts every other numbered draft.

Nothing is written unless every item of the reply makes sense: a typo in one item must
not half-apply a review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from vidette.eval.dataset import UNLABELLED, Entry, Labels

Action = Literal["accept", "set", "skip", "alert", "silent"]

ACCEPT = {"ok", "okay", "ок", "окей", "yes", "y", "+", "да", "верно", "правильно", "correct",
          "good", "ага", "✓", "👍"}  # fmt: skip
ALERT = {"alert", "alarm", "тревога", "тревожно", "тревогу", "!"}
SILENT = {"silent", "quiet", "тихо", "тишина", "норм", "нормально", "ложная", "ложно", "false"}
SKIP = {"?", "skip", "unlabelled", "unclear", "непонятно", "незнаю", "не знаю", "хз",
        "пропуск", "пропустить", "мусор", "drop"}  # fmt: skip
REST = {"rest", "others", "all", "остальные", "остальное", "прочие", "все", "всё"}

BUILTIN_ALIASES: dict[str, set[str]] = {
    "passer_by": {"passer", "passerby", "walker", "прохожий", "прохожая", "прохожие", "мимо"},
    "courier_at_door": {"courier", "delivery", "parcel", "курьер", "доставка", "посылка"},
    "visitor_at_door": {"visitor", "guest", "гость", "гости", "звонок", "звонит"},
    "door_handle_attempt": {"handle", "door", "ручка", "дверь", "дергает", "взлом"},
    "lingering_at_door": {
        "lingering", "loiter", "loitering", "топчется", "слоняется", "околачивается",
        "подозрительный", "заглядывает",
    },
    "household_member": {"household", "family", "me", "свой", "свои", "домашний", "мы", "я"},
    "cat": {"кот", "кошка", "котик"},
    "dog": {"собака", "пес", "пёс"},
    "animal": {"животное", "зверь"},
    "car_passing": {"car", "машина", "авто", "автомобиль"},
    "car_lights": {"headlights", "lights", "фары", "свет"},
    "night_noise": {"noise", "nothing", "шум", "ничего", "мошки", "дождь", "тени", "паук"},
}


class ReviewError(Exception):
    """The reply cannot be applied; the message lists every problem."""


@dataclass(frozen=True)
class Item:
    number: int
    action: Action
    scenario: str | None
    raw: str


@dataclass(frozen=True)
class Change:
    entry_id: str
    number: int
    old: str
    new: str
    accepted_draft: bool


def _normalize(text: str) -> str:
    return text.lower().replace("ё", "е").strip()


def vocabulary(labels: Labels) -> dict[str, str]:
    """Every word that names a scenario in this dataset → the scenario."""
    words: dict[str, str] = {}
    for scenario in labels.scenarios:
        words[_normalize(scenario)] = scenario
        words[_normalize(scenario.replace("_", " "))] = scenario
        for alias in BUILTIN_ALIASES.get(scenario, set()):
            words.setdefault(_normalize(alias), scenario)
    for alias, scenario in labels.aliases.items():
        words[_normalize(alias)] = scenario  # the dataset's own aliases win
    return words


def _classify(phrase: str, words: dict[str, str]) -> tuple[Action, str | None] | None:
    phrase = _normalize(phrase)
    if phrase in ACCEPT:
        return "accept", None
    if phrase in ALERT:
        return "alert", None
    if phrase in SILENT:
        return "silent", None
    if phrase in SKIP:
        return "skip", None
    if phrase in words:
        return "set", words[phrase]
    return None


_TOKEN_RE = re.compile(r"\S+")
_NUMBERED_RE = re.compile(r"^#?(\d+)[-:.)]?(.*)$")


def parse_reply(text: str, labels: Labels) -> tuple[list[Item], bool, list[str]]:
    """(items, rest_ok, problems). Groups start at each number: "3 курьер 7 тревога" works
    without commas, and so does "3-курьер"."""
    words = vocabulary(labels)
    groups: list[tuple[int | None, list[str]]] = []
    for segment in re.split(r"[,;\n]+", text):  # a comma always ends an item
        opened = False
        for token in _TOKEN_RE.findall(segment):
            match = _NUMBERED_RE.match(token)
            if match:
                groups.append((int(match.group(1)), [match.group(2)] if match.group(2) else []))
                opened = True
            elif opened:
                groups[-1][1].append(token)
            else:
                groups.append((None, [token]))
                opened = True
    items: list[Item] = []
    rest_ok = False
    problems: list[str] = []
    for number, tokens in groups:
        phrase = " ".join(tokens).strip(" .!")
        if number is None:
            parts = _normalize(phrase).split()
            agree = ACCEPT | {"норм", "нормально", "fine"}
            if parts and parts[0] in REST and len(parts) >= 2 and " ".join(parts[1:]) in agree:
                rest_ok = True
            else:
                problems.append(f"'{phrase}': start with a number, or say 'rest ok'")
            continue
        if not phrase:
            problems.append(f"{number}: what about it? ('{number} ok', '{number} courier')")
            continue
        found = _classify(phrase, words)
        if found is None:  # "3 это курьер": one recognizable word is enough
            hits = {
                hit for word in phrase.split() if (hit := _classify(word, words)) is not None
            }
            found = next(iter(hits)) if len(hits) == 1 else None
        if found is None:
            known = ", ".join(sorted(labels.scenarios))
            problems.append(f"{number}: '{phrase}' is not a scenario or alias ({known})")
            continue
        items.append(Item(number=number, action=found[0], scenario=found[1], raw=phrase))
    return items, rest_ok, problems


def resolve(labels: Labels, text: str) -> list[Change]:
    """Every change the reply asks for; ReviewError if any part is unclear."""
    items, rest_ok, problems = parse_reply(text, labels)
    by_number = {entry.number: entry for entry in labels.entries if entry.number is not None}
    policy = next(iter(labels.policies.values()))
    alerting = list(policy.alert_on)
    silent = [name for name in labels.scenarios if name not in alerting]
    changes: dict[str, Change] = {}
    mentioned: set[int] = set()

    def change(entry: Entry, new: str, accepted: bool) -> None:
        assert entry.number is not None
        changes[entry.id] = Change(entry.id, entry.number, entry.scenario, new, accepted)

    def proposal(entry: Entry) -> str | None:
        if entry.draft is None or entry.draft.scenario not in labels.scenarios:
            return None
        return entry.draft.scenario

    for item in items:
        entry = by_number.get(item.number)
        if entry is None:
            problems.append(f"{item.number}: no entry has that number")
            continue
        if item.number in mentioned:
            problems.append(f"{item.number}: mentioned twice")
            continue
        mentioned.add(item.number)
        proposed = proposal(entry)
        if item.action == "set":
            assert item.scenario is not None
            change(entry, item.scenario, accepted=item.scenario == proposed)
        elif item.action == "skip":
            change(entry, UNLABELLED, accepted=False)
        elif item.action == "accept":
            if proposed is None:
                problems.append(f"{item.number}: no draft to accept; name the scenario")
            else:
                change(entry, proposed, accepted=True)
        else:
            wanted = alerting if item.action == "alert" else silent
            if proposed in wanted:
                change(entry, proposed, accepted=True)
            elif len(wanted) == 1:
                change(entry, wanted[0], accepted=False)
            else:
                problems.append(
                    f"{item.number}: '{item.raw}' fits several scenarios, name one: "
                    + ", ".join(wanted)
                )
    if rest_ok:
        for number, entry in sorted(by_number.items()):
            if number in mentioned or entry.status != "draft":
                continue
            proposed = proposal(entry)
            if proposed is not None:
                change(entry, proposed, accepted=True)
    if problems:
        raise ReviewError("nothing applied:\n  - " + "\n  - ".join(problems))
    return sorted(changes.values(), key=lambda c: c.number)
