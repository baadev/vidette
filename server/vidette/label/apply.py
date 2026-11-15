"""Applying a review reply ("3 ok, 5 courier, 5.2 Robin") to a dataset: one code path for
`vidette label apply` and for answers given elsewhere (the Telegram buttons, ADR-0013).

Nothing is written unless the whole reply makes sense: scenario problems and face
problems are collected first and returned together as one `ReviewError`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from vidette.eval.dataset import LABELS_FILE, UNLABELLED, Entry, load_dataset
from vidette.label import labels_file
from vidette.label.people_reply import FaceTarget, resolve_faces, split_reply
from vidette.label.review import Change, ReviewError, resolve
from vidette.people.check import confirm
from vidette.people.engine import FaceEngine
from vidette.people.report import STRANGER, UNSURE
from vidette.people.sidecar import save_report
from vidette.people.store import PeopleStore


def pending(entry: Entry) -> bool:
    """On the next sheet: numbered, and either a draft or never labelled."""
    if entry.number is None:
        return False
    return entry.status == "draft" or (entry.status is None and entry.scenario == UNLABELLED)


@dataclass
class Applied:
    changes: list[Change] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)  # one per change, for people to read
    written: bool = False
    scored: int = 0  # entries scored after the change
    waiting: int = 0  # entries still waiting for review


def apply_reply(
    root: Path,
    reply: str,
    *,
    store: PeopleStore | None,
    engine: Callable[[], FaceEngine | None],
    dry_run: bool = False,
) -> Applied:
    """Apply `reply` to `root/labels.yaml` (and face answers to the clips' reports).
    `engine` is called only when a face answer names a person. Raises ReviewError."""
    dataset = load_dataset(root)
    answers, rest = split_reply(reply, store)
    targets, face_problems = resolve_faces(dataset.labels, root, answers)
    try:
        changes = resolve(dataset.labels, rest) if rest.strip() else []
    except ReviewError as exc:
        if face_problems:
            raise ReviewError("\n  - ".join([str(exc), *face_problems])) from None
        raise
    if face_problems:
        raise ReviewError("nothing applied:\n  - " + "\n  - ".join(face_problems))
    result = Applied(changes=changes)
    if targets and store is not None:
        result.lines += _faces(root, targets, store, engine, dry_run=dry_run)
    if not changes:
        return result
    text = (root / LABELS_FILE).read_text(encoding="utf-8")
    by_id = {entry.id: entry for entry in dataset.labels.entries}
    for change in changes:
        entry = by_id[change.entry_id]
        updated = entry.model_copy(update={"scenario": change.new, "status": "reviewed"})
        text = labels_file.replace_entry(
            text, entry.id, labels_file.render_entry(labels_file.entry_dict(updated))
        )
        verb = "accepted" if change.accepted_draft else "set"
        was = "" if change.accepted_draft or change.old == change.new else f" (was {change.old})"
        result.lines.append(f"#{change.number} {change.entry_id}: {verb} {change.new}{was}")
    if dry_run:
        return result
    labels_file.write_validated(root, text)
    after = load_dataset(root)
    result.written = True
    result.scored = len(after.scored_entries)
    result.waiting = sum(1 for entry in after.labels.entries if pending(entry))
    return result


def _faces(
    root: Path,
    targets: list[FaceTarget],
    store: PeopleStore,
    engine: Callable[[], FaceEngine | None],
    *,
    dry_run: bool,
) -> list[str]:
    if dry_run:
        return [
            f"#{t.answer.number}.{t.face} {t.entry_id}: would be {t.answer.answer}" for t in targets
        ]
    loaded = None
    if any(t.answer.answer not in (STRANGER, UNSURE) for t in targets):
        loaded = engine()
    lines = []
    for target in targets:
        done = confirm(target.report, target.face, target.answer.answer, store=store,
                       engine=loaded, media_root=root, where=f"clip:{target.entry_id}")  # fmt: skip
        save_report(target.clip, target.report)
        lines.append(f"#{target.answer.number}.{target.face} {target.entry_id}: {done.message}")
    return lines
