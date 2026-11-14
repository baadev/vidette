"""`vidette label draft | sheets | apply`: machine drafts, contact sheets, review by number.

    vidette label draft  ~/vidette-data/front-door     # new clips → draft entries + sheets
    vidette label apply  ~/vidette-data/front-door "3 ok, 5 courier, 7 alert, rest ok"
    vidette label sheets ~/vidette-data/front-door     # redraw sheets for pending entries

Docs: docs/label.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from vidette.collect.clips import load_meta
from vidette.collect.collector import STATE_DIR, STATE_FILE
from vidette.core.config import ConfigError, VlmBackend
from vidette.eval.dataset import (
    LABELS_FILE,
    UNLABELLED,
    VIDEO_SUFFIXES,
    Dataset,
    DatasetError,
    Entry,
    load_dataset,
)
from vidette.ffmpeg import MediaToolError
from vidette.label import labels_file
from vidette.label.drafting import UNCLEAR, DraftError, draft_label, first_policy_alert
from vidette.label.people_reply import FaceTarget, resolve_faces, split_reply
from vidette.label.review import ReviewError, resolve
from vidette.label.sheet import SheetItem, render_sheet, sheet_faces
from vidette.people.check import PeopleCheck, confirm
from vidette.people.engine import EngineUnavailableError
from vidette.people.models import ModelError
from vidette.people.report import STRANGER, UNSURE
from vidette.people.setup import check_for, engine_for, store_for
from vidette.people.sidecar import crops_for, load_report, save_report
from vidette.people.store import PeopleStore, StoreError
from vidette.vlm.budget import FileLedger, default_ledger_path
from vidette.vlm.client import VlmClient
from vidette.vlm.endpoint import VlmUnavailableError, endpoint_for
from vidette.vlm.setup import chosen_backend, cli_config

REVIEW_DIR = "review"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="label_command", required=True)
    draft = actions.add_parser(
        "draft",
        help="draft entries for new clips with a VLM, then draw contact sheets",
        description="Every clip under DATASET/clips without an entry becomes a numbered "
        "entry in labels.yaml with status: draft and the model's proposal (scenario, "
        "alert/silent, one line, confidence). Drafts are not ground truth until reviewed.",
    )
    draft.add_argument("dataset", type=Path)
    draft.add_argument(
        "--backend",
        choices=["local", "cloud"],
        default=None,
        help="default: the System page's choice, then the config, then local",
    )
    draft.add_argument(
        "--config",
        type=Path,
        default=None,
        help="default $VIDETTE_CONFIG, then ~/.config/vidette/vidette.yaml",
    )
    draft.add_argument("--limit", type=int, default=50, help="at most this many clips per run")
    draft.add_argument("--per-sheet", type=int, default=8, choices=range(8, 13),
                       metavar="8-12", help="clips per contact sheet")  # fmt: skip
    draft.add_argument("--no-vlm", action="store_true",
                       help="add numbered entries without drafts (label them by hand)")  # fmt: skip
    draft.add_argument("--people", action=argparse.BooleanOptionalAction, default=None,
                       help="check each clip for known people first and tell the model "
                       "who was recognized (default: people.enabled in the config)")  # fmt: skip

    sheets = actions.add_parser("sheets", help="redraw contact sheets for pending entries")
    sheets.add_argument("dataset", type=Path)
    sheets.add_argument("--per-sheet", type=int, default=8, choices=range(8, 13), metavar="8-12")

    apply = actions.add_parser(
        "apply",
        help="apply a review reply: '3 ok, 5 courier, 7 alert, rest ok'",
        description="Corrections by review number. Words: ok (accept the draft), a "
        "scenario or alias (set it), alert / silent (accept or pick the only fitting "
        "scenario), ? (leave unlabelled), rest ok (accept every other draft). Russian "
        "works too: 'курьер', 'тревога', 'остальные ок'. Nothing is written unless the "
        "whole reply makes sense.",
    )
    apply.add_argument("dataset", type=Path)
    apply.add_argument("reply", help="the reply text, or - to read it from stdin")
    apply.add_argument("--dry-run", action="store_true", help="show the changes, write nothing")
    apply.add_argument("--config", type=Path, default=None,
                       help="config with people.dir, for face answers ('5 Robin')")  # fmt: skip



def run(args: argparse.Namespace) -> int:
    root = Path(args.dataset).expanduser().resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2
    try:
        if args.label_command == "draft":
            return asyncio.run(_draft(root, args))
        if args.label_command == "sheets":
            dataset = load_dataset(root)
            pending = [e for e in dataset.labels.entries if _pending(e)]
            paths = asyncio.run(_sheets(dataset, pending, args.per_sheet))
            _print_sheets(root, paths, len(pending))
            return 0
        return _apply(root, args.reply, dry_run=args.dry_run, config_path=args.config)
    except (
        DatasetError, ConfigError, VlmUnavailableError, MediaToolError, ValueError, StoreError,
    ) as exc:  # fmt: skip
        print(f"error: {exc}", file=sys.stderr)
        return 2


# --- draft ---------------------------------------------------------------------------


def watched_hours(root: Path) -> float | None:
    try:
        data = json.loads((root / STATE_DIR / STATE_FILE).read_text(encoding="utf-8"))
        return float(data["watched_seconds"]) / 3600
    except (OSError, ValueError, KeyError, TypeError):
        return None


def new_clips(dataset: Dataset) -> list[Path]:
    referenced = {item for entry in dataset.labels.entries for item in entry.media}
    clips = sorted(
        path
        for path in (dataset.root / "clips").glob("*")
        if path.suffix.lower() in VIDEO_SUFFIXES
        and path.relative_to(dataset.root).as_posix() not in referenced
    )
    return clips


def clip_entry(root: Path, clip: Path, number: int) -> Entry:
    meta = load_meta(clip.with_suffix(".json"))
    media = [clip.relative_to(root).as_posix()]
    if meta is not None:
        media += [frame for frame in meta.frames if (root / frame).is_file()]
        return Entry(
            id=meta.id,
            scenario=UNLABELLED,
            number=number,
            camera=meta.camera,
            at=meta.started_at,
            duration_s=meta.duration_s,
            media=media,
        )
    frames_dir = root / "frames" / clip.stem
    if frames_dir.is_dir():
        media += sorted(
            (p.relative_to(root).as_posix() for p in frames_dir.glob("*.jpg")),
            key=lambda name: (len(name), name),
        )
    entry_id = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in clip.stem)
    return Entry(id=entry_id.strip("-.") or f"clip-{number}", scenario=UNLABELLED,
                 number=number, media=media)  # fmt: skip


@dataclass
class _Totals:
    drafted: int = 0
    without: int = 0
    usd: float | None = 0.0
    latency: float = 0.0


async def _draft(root: Path, args: argparse.Namespace) -> int:
    if not (root / LABELS_FILE).exists():
        labels_file.create(root, observed_hours=watched_hours(root))
        print(f"created {root / LABELS_FILE} (starter vocabulary, policy entry-interest)")
    text = (root / LABELS_FILE).read_text(encoding="utf-8")
    hours = watched_hours(root)
    if hours:
        text, changed = labels_file.set_observed_hours(text, hours)
        if changed:
            labels_file.write_validated(root, text)
            print(f"observed_hours: {hours:.2f} (watched time from vidette collect)")
    dataset = load_dataset(root)
    clips = new_clips(dataset)
    if not clips:
        print("no new clips: every clip under clips/ already has an entry")
        return 0
    clips = clips[: max(0, args.limit)]
    next_number = max((e.number or 0 for e in dataset.labels.entries), default=0) + 1
    ids = {entry.id for entry in dataset.labels.entries}

    client: VlmClient | None = None
    judge_name = ""
    people = _people_check(args)
    if not args.no_vlm:
        config, source = cli_config(args.config)
        backend = chosen_backend(config, args.backend)
        endpoint = endpoint_for(config, backend)
        client = VlmClient(endpoint, ledger=FileLedger(default_ledger_path()))
        judge_name = f"vlm-{backend.value}"
        where = "built-in defaults" if source is None else str(source)
        print(f"drafting with {judge_name}: {endpoint.model} at {endpoint.host} ({where})")
        print(f"  {endpoint.privacy}")
        if backend is VlmBackend.cloud and not endpoint.prices.is_set:
            print("  cost: unknown (no prices configured for the cloud backend)")

    totals = _Totals()
    blocks: list[str] = []
    added: list[Entry] = []
    frames = 4
    try:
        for clip in clips:
            entry = clip_entry(root, clip, next_number)
            if entry.id in ids:
                print(f"  skip {clip.name}: entry id {entry.id} already exists")
                continue
            ids.add(entry.id)
            next_number += 1
            case = dataset.case(entry)
            if people is not None:
                try:
                    report = await people.check(case, crops_for(clip))
                except (MediaToolError, OSError, StoreError) as exc:
                    print(f"  #{entry.number} {entry.id}: no face check ({exc})")
                else:
                    save_report(clip, report)
                    case = replace(case, people=report)
                    print(f"  #{entry.number} {entry.id}: faces: {report.summary()}")
            if client is not None:
                try:
                    result = await draft_label(
                        client, dataset.labels, case, frames=frames, judge_name=judge_name,
                    )  # fmt: skip
                except DraftError as exc:
                    print(f"  #{entry.number} {entry.id}: no draft ({exc})")
                    if exc.fatal:
                        print("  stopping drafts for this run; the rest get numbers only")
                        await client.aclose()
                        client = None
                else:
                    scenario = result.draft.scenario
                    entry = entry.model_copy(
                        update={
                            "scenario": scenario if scenario != UNCLEAR else UNLABELLED,
                            "status": "draft",
                            "draft": result.draft,
                        }
                    )
                    totals.drafted += 1
                    totals.latency += result.latency_s
                    if result.cost_usd is None or totals.usd is None:
                        totals.usd = None
                    else:
                        totals.usd += result.cost_usd
                    alert = {True: "ALERT", False: "silent", None: "?"}[result.draft.alert]
                    print(
                        f"  #{entry.number} {entry.id}: {scenario} ({alert}, "
                        f"{(result.draft.confidence or 0) * 100:.0f}%) "
                        f"{result.draft.description or ''}"
                    )
            if entry.draft is None:
                totals.without += 1
            blocks.append(labels_file.render_entry(labels_file.entry_dict(entry)))
            added.append(entry)
    finally:
        if client is not None:
            await client.aclose()

    if not blocks:
        return 0
    text = (root / LABELS_FILE).read_text(encoding="utf-8")
    labels_file.write_validated(root, labels_file.append_entries(text, blocks))
    first, last = added[0].number, added[-1].number
    print(
        f"added {len(added)} entries (#{first}-#{last}) to {LABELS_FILE}: "
        f"{totals.drafted} drafted, {totals.without} without a draft"
    )
    if totals.drafted:
        cost = "unknown" if totals.usd is None else f"${totals.usd:.4f}"
        print(f"  VLM: {cost}, {totals.latency / totals.drafted:.1f} s per clip on average")
    dataset = load_dataset(root)
    numbers = {entry.number for entry in added}
    fresh = [e for e in dataset.labels.entries if e.number in numbers]
    paths = await _sheets(dataset, fresh, args.per_sheet)
    _print_sheets(root, paths, len(fresh))
    return 0


def _people_check(args: argparse.Namespace) -> PeopleCheck | None:
    """The face check for `label draft`, or None (off, or not available: said once)."""
    config, _source = cli_config(args.config)
    wanted = config.people.enabled if args.people is None else args.people
    if not wanted:
        return None
    try:
        check = check_for(config.people, download=True)
    except (StoreError, ModelError, EngineUnavailableError) as exc:
        print(f"known people: off for this run ({exc})")
        return None
    enrolled = check.store.people()
    if not enrolled:
        print(f"known people: nobody enrolled in {check.store.root}; skipping face checks")
        return None
    print(f"known people: checking faces for {', '.join(p.name for p in enrolled)} "
          "(on this machine; the model hears only names it recognized)")  # fmt: skip
    return check


# --- sheets --------------------------------------------------------------------------


def _pending(entry: Entry) -> bool:
    """On the next sheet: numbered, and either a draft or never labelled."""
    if entry.number is None:
        return False
    return entry.status == "draft" or (entry.status is None and entry.scenario == UNLABELLED)


def _sheet_item(dataset: Dataset, entry: Entry) -> SheetItem:
    assert entry.number is not None
    draft = entry.draft
    proposal = None if draft is None else draft.scenario
    alert = None if draft is None else first_policy_alert(dataset.labels, draft.scenario)
    clip = next(
        (dataset.root / m for m in entry.media if Path(m).suffix.lower() in VIDEO_SUFFIXES),
        None,
    )
    report = None if clip is None else load_report(clip)
    return SheetItem(
        number=entry.number,
        entry_id=entry.id,
        case=dataset.case(entry),
        proposal=proposal,
        alert=alert,
        confidence=None if draft is None else draft.confidence,
        description=None if draft is None else draft.description,
        at=entry.at,
        duration_s=entry.duration_s,
        camera=entry.camera,
        faces=None if report is None else sheet_faces(report),
    )


async def _sheets(dataset: Dataset, entries: list[Entry], per_sheet: int) -> list[Path]:
    review = dataset.root / REVIEW_DIR
    review.mkdir(exist_ok=True)
    ordered = sorted(entries, key=lambda e: e.number or 0)
    paths: list[Path] = []
    for start in range(0, len(ordered), per_sheet):
        chunk = ordered[start : start + per_sheet]
        first, last = chunk[0].number or 0, chunk[-1].number or 0
        title = f"{dataset.labels.name}  #{first}-#{last}  {datetime.now():%d.%m %H:%M}"
        png = await render_sheet([_sheet_item(dataset, e) for e in chunk], title=title)
        path = review / f"sheet-{first:04d}-{last:04d}.png"
        path.write_bytes(png)
        paths.append(path)
    return paths


def _print_sheets(root: Path, paths: list[Path], count: int) -> None:
    if not paths:
        print("nothing to review")
        return
    print(f"{count} entries to review on {len(paths)} contact sheet(s):")
    for path in paths:
        print(f"  {path}")
    print(f'reply with: vidette label apply {root} "3 ok, 5 courier, 7 alert, rest ok"')


# --- apply ---------------------------------------------------------------------------


def _people_store(config_path: Path | None) -> PeopleStore | None:
    config, _source = cli_config(config_path)
    try:
        store = store_for(config.people)
    except StoreError:
        return None
    return store if store.people() else None


def _apply_faces(
    root: Path, targets: list[FaceTarget], store: PeopleStore, config_path: Path | None
) -> None:
    engine = None
    if any(t.answer.answer not in (STRANGER, UNSURE) for t in targets):
        config, _source = cli_config(config_path)
        engine = engine_for(config.people, download=True)
    for target in targets:
        done = confirm(target.report, target.face, target.answer.answer, store=store,
                       engine=engine, media_root=root, where=f"clip:{target.entry_id}")  # fmt: skip
        save_report(target.clip, target.report)
        print(f"  #{target.answer.number}.{target.face} {target.entry_id}: {done.message}")


def _apply(root: Path, reply: str, *, dry_run: bool, config_path: Path | None = None) -> int:
    if reply == "-":
        reply = sys.stdin.read()
    dataset = load_dataset(root)
    store = _people_store(config_path)
    answers, rest = split_reply(reply, store)
    targets, face_problems = resolve_faces(dataset.labels, root, answers)
    try:
        changes = resolve(dataset.labels, rest) if rest.strip() else []
    except ReviewError as exc:
        print(f"error: {exc}", file=sys.stderr)
        for problem in face_problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    if face_problems:
        print("error: nothing applied:\n  - " + "\n  - ".join(face_problems), file=sys.stderr)
        return 1
    if targets and store is not None:
        if dry_run:
            for target in targets:
                print(f"  #{target.answer.number}.{target.face} {target.entry_id}: would be "
                      f"{target.answer.answer}")  # fmt: skip
        else:
            _apply_faces(root, targets, store, config_path)
    if not changes:
        if not targets:
            print("nothing to change")
        return 0
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
        print(f"  #{change.number} {change.entry_id}: {verb} {change.new}{was}")
    if dry_run:
        print("dry run: nothing written")
        return 0
    labels_file.write_validated(root, text)
    after = load_dataset(root)
    left = sum(1 for entry in after.labels.entries if _pending(entry))
    print(
        f"{len(changes)} entries reviewed; {len(after.scored_entries)} scored now, "
        f"{left} still waiting for review"
    )
    return 0
