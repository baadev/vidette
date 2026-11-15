"""Answers about a live verdict become reviewed labels in the clip's dataset.

A person who taps "right", "false alarm", "do not know" or "one of us" under an alert
(docs/telegram.md) has just labelled a clip. If the clip lives in a dataset folder
(`<dataset>/clips/<id>.mp4`, what `vidette collect` writes), the answer goes into that
dataset's `labels.yaml` through the same code as `vidette label apply` (ADR-0013):

1. `labels.yaml` is created from the starter template if the folder has none;
2. the policy's scenarios that the vocabulary lacks are appended to `scenarios:`, with
   `other` (none of the policy's scenarios);
3. a clip without an entry gets one, numbered like `vidette label draft` would, with the
   live verdict as its `draft` (so the report can count accepted drafts);
4. the answer becomes a reply by number and is applied; the entry is `reviewed`.

| Answer | Reply | Truth |
|---|---|---|
| right | `N <the judge's scenario>` | what the judge named |
| wrong | `N other` | none of the policy's scenarios (`N ?` if it was not an alert) |
| unsure | `N ?` | unlabelled, reviewed, not scored |
| ours | `N household_member` | someone who lives here (when that scenario exists) |
| wrong + reason | the reason's scenario if the policy has it | `passing` → `passer_by` |
| wrong + `unimportant` | `N <the judge's scenario>` | it was right, just not worth it |

A reason is also written as the entry's `reason`. Every answer returns what the entry was
before (`Recorded.previous`), and `restore` puts exactly that back: an answer can be
taken back from the alert.

Every write is validated before `labels.yaml` is replaced (`labels.yaml.bak` keeps the
previous version). Nothing else in the folder is touched.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from vidette.core.config import OTHER_SCENARIO
from vidette.eval.dataset import LABELS_FILE, DraftInfo, Entry, load_dataset
from vidette.label import labels_file
from vidette.label.apply import Applied, apply_reply
from vidette.label.entries import clip_entry, watched_hours

Answer = Literal["right", "wrong", "unsure", "ours"]
HOUSEHOLD = "household_member"
OTHER_MEANING = "none of the policy's scenarios (the alert was false)"
_LOCK = threading.Lock()  # one labels.yaml edit at a time in this process


class FeedbackError(Exception):
    """The answer cannot become a label; the message says why (the event keeps it)."""


@dataclass(frozen=True)
class LiveVerdict:
    by: str  # judge and model, e.g. "vlm-local qwen2.5vl:7b (live)"
    scenario: str | None  # the policy scenario the judge named
    alert: bool
    description: str | None = None
    confidence: float | None = None
    at: datetime | None = None


@dataclass(frozen=True)
class Recorded:
    root: Path
    entry_id: str
    number: int
    reply: str
    applied: Applied
    # the entry before this answer (scenario, status, reason), for `restore`; `created`
    # when the answer added the entry (restoring leaves it as the draft it was)
    previous: dict[str, str | None]


JUDGES_SCENARIO = "*"  # in a reason's scenarios: the judge was right, keep its scenario


def dataset_root(clip: Path) -> Path | None:
    """`<dataset>/clips/<id>.mp4` → `<dataset>`; any other layout has no dataset."""
    return clip.parent.parent if clip.parent.name == "clips" else None


def ensure_vocabulary(root: Path, scenarios: dict[str, str]) -> None:
    """labels.yaml exists and knows every scenario in `scenarios` plus `other`."""
    path = root / LABELS_FILE
    if not path.exists():
        labels_file.create(root, observed_hours=watched_hours(root))
    text = path.read_text(encoding="utf-8")
    wanted = {**scenarios, OTHER_SCENARIO: OTHER_MEANING}
    updated = labels_file.add_scenarios(text, wanted)
    if updated != text:
        labels_file.write_validated(root, updated)


def ensure_entry(root: Path, clip: Path, live: LiveVerdict) -> Entry:
    """The clip's entry, appended with the live verdict as its draft when missing."""
    dataset = load_dataset(root)
    relative = clip.relative_to(root).as_posix()
    for entry in dataset.labels.entries:
        if relative in entry.media or entry.id == clip.stem:
            if entry.number is None:
                raise FeedbackError(
                    f"entry {entry.id} in {root / LABELS_FILE} has no review number; "
                    "label it there by hand"
                )
            return entry
    number = max((e.number or 0 for e in dataset.labels.entries), default=0) + 1
    entry = clip_entry(root, clip, number)
    vocabulary = dataset.labels.scenarios
    proposal = live.scenario if live.scenario in vocabulary else "unclear"
    entry = entry.model_copy(
        update={
            "scenario": proposal if proposal in vocabulary else entry.scenario,
            "status": "draft",
            "draft": DraftInfo(
                by=live.by,
                at=live.at,
                scenario=proposal,
                alert=live.alert,
                description=live.description,
                confidence=live.confidence,
            ),
        }
    )
    text = (root / LABELS_FILE).read_text(encoding="utf-8")
    block = labels_file.render_entry(labels_file.entry_dict(entry))
    labels_file.write_validated(root, labels_file.append_entries(text, [block]))
    return entry


def reply_for(
    answer: Answer,
    number: int,
    live: LiveVerdict,
    vocabulary: set[str],
    truth: tuple[str, ...] = (),
) -> str:
    """The review reply for an answer. `truth`: for a false alarm with a reason, the
    scenarios that reason stands for, the first one the vocabulary knows wins
    (`JUDGES_SCENARIO`: the judge named it right, it was just not worth an alert)."""
    if answer == "wrong" and truth:
        for scenario in truth:
            if scenario == JUDGES_SCENARIO and live.scenario in vocabulary:
                return f"{number} {live.scenario}"
            if scenario in vocabulary:
                return f"{number} {scenario}"
    if answer == "right":
        if live.scenario is None or live.scenario not in vocabulary:
            raise FeedbackError("the judge named no scenario this dataset knows")
        return f"{number} {live.scenario}"
    if answer == "wrong":
        # A false alarm is "none of the scenarios"; a wrong "not an alert" (only a test
        # shows buttons on those) has an unknown truth: reviewed, left unlabelled.
        return f"{number} {OTHER_SCENARIO}" if live.alert else f"{number} ?"
    if answer == "unsure":
        return f"{number} ?"
    if HOUSEHOLD not in vocabulary:
        raise FeedbackError(f"the dataset has no '{HOUSEHOLD}' scenario")
    return f"{number} {HOUSEHOLD}"


def record(
    clip: Path,
    live: LiveVerdict,
    answer: Answer,
    *,
    scenarios: dict[str, str],
    reason: str | None = None,
    truth: tuple[str, ...] = (),
    note: str | None = None,
) -> Recorded:
    """Write one answer about `clip` into its dataset (blocking; run in a thread)."""
    root = dataset_root(clip)
    if root is None:
        raise FeedbackError(f"{clip.name} is not in a dataset folder (<dataset>/clips/)")
    if not clip.is_file():
        raise FeedbackError(f"{clip.name} is gone from {clip.parent}")
    with _LOCK:
        try:
            ensure_vocabulary(root, scenarios)
            before = {e.id for e in load_dataset(root).labels.entries}
            entry = ensure_entry(root, clip, live)
            assert entry.number is not None
            previous: dict[str, str | None] = {
                "scenario": entry.scenario,
                "status": entry.status,
                "reason": entry.reason,
                "notes": entry.notes,
                "created": "yes" if entry.id not in before else None,
            }
            vocabulary = set(load_dataset(root).labels.scenarios)
            reply = reply_for(answer, entry.number, live, vocabulary, truth)
            applied = apply_reply(root, reply, store=None, engine=lambda: None)
            if reason != entry.reason or (note is not None and note != entry.notes):
                _set_fields(root, entry.id, reason=reason, notes=note or entry.notes)
        except FeedbackError:
            raise
        except Exception as exc:  # a dataset problem must not take the bot down
            raise FeedbackError(f"{type(exc).__name__}: {exc}"[:300]) from exc
    return Recorded(root, entry.id, entry.number, reply, applied, previous)


def _set_fields(root: Path, entry_id: str, **fields: str | None) -> None:
    dataset = load_dataset(root)
    entry = next(e for e in dataset.labels.entries if e.id == entry_id)
    updated = entry.model_copy(update=fields)
    text = (root / LABELS_FILE).read_text(encoding="utf-8")
    text = labels_file.replace_entry(
        text, entry_id, labels_file.render_entry(labels_file.entry_dict(updated))
    )
    labels_file.write_validated(root, text)


def restore(clip: Path, previous: dict[str, str | None]) -> None:
    """Put a clip's entry back the way it was before an answer (`Recorded.previous`):
    its scenario, status, reason and notes. An entry the answer created goes back to
    being the model's draft. Blocking; run in a thread."""
    root = dataset_root(clip)
    if root is None:
        raise FeedbackError(f"{clip.name} is not in a dataset folder (<dataset>/clips/)")
    with _LOCK:
        try:
            dataset = load_dataset(root)
            relative = clip.relative_to(root).as_posix()
            entry = next(
                (e for e in dataset.labels.entries if relative in e.media or e.id == clip.stem),
                None,
            )
            if entry is None:
                return
            scenario = previous.get("scenario") or entry.scenario
            if scenario not in dataset.labels.scenarios and scenario != "unlabelled":
                scenario = entry.scenario
            status = previous.get("status")
            updated = entry.model_copy(
                update={
                    "scenario": scenario,
                    "status": status if status in ("draft", "reviewed") else None,
                    "reason": previous.get("reason"),
                    "notes": previous.get("notes"),
                }
            )
            text = (root / LABELS_FILE).read_text(encoding="utf-8")
            text = labels_file.replace_entry(
                text, entry.id, labels_file.render_entry(labels_file.entry_dict(updated))
            )
            labels_file.write_validated(root, text)
        except Exception as exc:  # a dataset problem must not take the bot down
            raise FeedbackError(f"{type(exc).__name__}: {exc}"[:300]) from exc
