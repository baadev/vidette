"""`vidette people`: enroll known people, check clips for them, confirm what was seen.

    vidette people models                         # download + verify the two model files
    vidette people add Sam selfie.jpg grid.png   # every readable face in the photos
    vidette people list
    vidette people match clip.mp4 | frames/ | a.jpg b.jpg
    vidette people scan ~/vidette-data/porch       # every clip of a dataset: report + crops
    vidette people confirm ~/vidette-data/porch/clips/20261114-210304.mp4 2 Robin
    vidette people sheet Sam out.jpg             # the reference faces, for a look
    vidette people remove Sam

The folder comes from `people.dir` in the config (default `$VIDETTE_DATA/people`, else
~/vidette-data/people) or `--dir`; it must be outside any git repository. Docs:
docs/people.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from vidette.core.config import ConfigError, PeopleConfig
from vidette.ffmpeg import MediaToolError
from vidette.people.check import PeopleCheck, confirm, crop_writer
from vidette.people.engine import EngineUnavailableError, FaceEngine
from vidette.people.models import MODELS, ModelError, default_models_dir
from vidette.people.report import STRANGER, UNSURE, PeopleReport
from vidette.people.setup import check_for, engine_for, store_for
from vidette.people.sidecar import crops_for, dataset_root, load_report, save_report
from vidette.people.store import NewFace, PeopleStore, StoreError
from vidette.verdicts.base import Case
from vidette.vlm.setup import cli_config

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv"})
ENROLL_MAX_SIDE = 1280  # YuNet finds faces up to ~300 px; a selfie's face is bigger

STRANGER_WORDS = {
    "stranger",
    "unknown",
    "чужой",
    "чужая",
    "чужие",
    "незнакомец",
    "незнакомка",
    "посторонний",
    "посторонняя",
    "не наш",
    "не наша",
}
UNSURE_WORDS = {"unsure", "?", "не знаю", "незнаю", "хз", "непонятно", "skip"}


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="config with a people: section (default $VIDETTE_CONFIG, then "
        "~/.config/vidette/vidette.yaml)",
    )
    parser.add_argument(
        "--dir", type=Path, default=None, help="people folder (overrides people.dir)"
    )
    actions = parser.add_subparsers(dest="people_command", required=True)

    actions.add_parser("models", help="download and verify the face models (about 39 MB)")

    add = actions.add_parser(
        "add",
        help="enroll a person from photos (a selfie, or a screenshot grid of many faces)",
        description="Every readable face in the photos becomes a reference for NAME. "
        "Duplicates are skipped; a face much closer to another enrolled person, or (in a "
        "batch) unlike every other face, is skipped as someone else.",
    )
    add.add_argument("name")
    add.add_argument("photos", nargs="+", type=Path)
    add.add_argument(
        "--keep-outliers",
        action="store_true",
        help="keep faces that look unlike the rest of the batch",
    )

    actions.add_parser("list", help="who is enrolled, with how many faces")

    remove = actions.add_parser("remove", help="delete a person: photos, faces, embeddings")
    remove.add_argument("name")
    remove.add_argument("--yes", action="store_true", help="do not ask")

    match = actions.add_parser(
        "match", help="check clips or images for known people (prints, writes nothing)"
    )
    match.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="video files, image files, or folders of images (one clip each)",
    )
    match.add_argument(
        "--crops", type=Path, default=None, help="save the face crops here (outside any repository)"
    )
    match.add_argument("--json", action="store_true")

    scan = actions.add_parser(
        "scan",
        help="check every clip of a dataset; reports go next to the clips, crops to faces/",
    )
    scan.add_argument("dataset", type=Path)
    scan.add_argument("--all", action="store_true", help="also clips checked before")
    scan.add_argument("--limit", type=int, default=0, help="at most this many clips")
    scan.add_argument(
        "--clip", action="append", default=None, help="only this clip id (repeatable)"
    )
    scan.add_argument(
        "--since",
        default=None,
        help="only clips with an id at or after this one, e.g. 20261114-2100",
    )

    conf = actions.add_parser(
        "confirm",
        help="answer for a face found in a clip: NAME, stranger or unsure",
        description="'this is Robin' adds the face to Robin's references; 'stranger' on "
        "a face that matched someone raises that person's threshold; 'unsure' records "
        "the answer only.",
    )
    conf.add_argument("clip", type=Path)
    conf.add_argument("face", type=int, help="the face number in the clip's report")
    conf.add_argument("answer", help="a person's name, 'stranger' or 'unsure'")

    sheet = actions.add_parser("sheet", help="draw a person's reference faces into one image")
    sheet.add_argument("name")
    sheet.add_argument("out", type=Path)


def _config(args: argparse.Namespace) -> PeopleConfig:
    config, _source = cli_config(args.config)
    people = config.people
    if args.dir is not None:
        people = people.model_copy(update={"dir": args.dir.expanduser()})
    return people


def _say(line: str) -> None:
    print(line, flush=True)


def run(args: argparse.Namespace) -> int:
    try:
        config = _config(args)
        command = args.people_command
        if command == "models":
            return _models(config)
        if command == "list":
            return _list(store_for(config))
        if command == "remove":
            return _remove(store_for(config), args.name, yes=args.yes)
        if command == "add":
            return _add(config, args.name, args.photos, keep_outliers=args.keep_outliers)
        if command == "match":
            return asyncio.run(_match(config, args.inputs, args.crops, as_json=args.json))
        if command == "scan":
            return asyncio.run(_scan(config, args))
        if command == "confirm":
            return _confirm(config, args.clip, args.face, args.answer)
        if command == "sheet":
            return _sheet(config, args.name, args.out)
    except (ConfigError, StoreError, ModelError, EngineUnavailableError, MediaToolError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


# --- models, list, remove ----------------------------------------------------------------


def _models(config: PeopleConfig) -> int:
    folder = config.models_dir or default_models_dir()
    print(f"face models in {folder}:")
    engine_for(config, download=True, say=_say)
    for spec in MODELS:
        print(f"  ok  {spec.name}  {spec.license}  sha256 {spec.sha256[:16]}…")
    return 0


def _list(store: PeopleStore) -> int:
    people = store.people()
    print(f"people folder: {store.root}")
    for warning in store.permission_warnings():
        print(f"  ⚠ {warning}")
    if not people:
        print("  nobody enrolled yet: vidette people add NAME PHOTO…")
    for person in people:
        view = store.view(person)
        rejected = f", {view.rejected} wrong match(es) confirmed" if view.rejected else ""
        print(
            f"  {view.name} ({view.id}): {view.enrolled} enrolled + {view.confirmed} confirmed "
            f"faces from {view.photos} photo(s); threshold {view.threshold:.2f}{rejected}"
        )
    for folder in store.not_enrolled():
        print(f"  {folder}/: files but not enrolled (vidette people add {folder} …)")
    return 0


def _remove(store: PeopleStore, name: str, *, yes: bool) -> int:
    person = store.require(name)
    view = store.view(person)
    if not yes:
        answer = input(
            f"delete {person.name}: {view.enrolled + view.confirmed} faces, {view.photos} "
            "photos? This cannot be undone. [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes", "д", "да"):
            print("nothing deleted")
            return 1
    store.delete(person.id)
    print(f"deleted {person.name} and everything stored for them")
    return 0


# --- add ---------------------------------------------------------------------------------


def _add(config: PeopleConfig, name: str, photos: list[Path], *, keep_outliers: bool) -> int:
    store = store_for(config)
    missing = [str(p) for p in photos if not p.is_file()]
    if missing:
        print(f"error: not a file: {', '.join(missing)}", file=sys.stderr)
        return 2
    engine = engine_for(config, download=True, say=_say)
    person = store.create(name)
    print(f"{person.name} ({person.id}) in {store.root}")
    faces: list[NewFace] = []
    for photo in photos:
        data = photo.read_bytes()
        try:
            found = engine.faces(data, max_side=ENROLL_MAX_SIDE)
        except ValueError as exc:
            print(f"  {photo.name}: skipped ({exc})")
            continue
        kept = store.add_photo(person.id, data, photo.suffix)
        readable = [face for face in found if face.readable and face.embedding and face.crop]
        unclear = len(found) - len(readable)
        note = f", {unclear} too small or unclear" if unclear else ""
        print(f"  {photo.name}: {len(found)} face(s), {len(readable)} readable{note}")
        for index, face in enumerate(readable, start=1):
            assert face.embedding is not None and face.crop is not None
            faces.append(NewFace(face.embedding, face.crop, face.face_px, f"photo:{kept}#{index}"))
    if not faces:
        print(
            "no readable face found: use a photo where the face is at least "
            f"{config.min_face_px} px tall and turned to the camera"
        )
        return 1
    result = store.add_faces(person.id, faces, kind="enrolled", check_outliers=not keep_outliers)
    view = store.view(store.require(person.id))
    print(
        f"added {len(result.added)} face(s); {result.duplicates} duplicate(s) skipped; "
        f"{person.name} now has {view.enrolled + view.confirmed} reference faces"
    )
    for source in result.outliers:
        print(
            f"  skipped {source}: unlike every other face (someone else?); "
            "--keep-outliers keeps such faces"
        )
    for source, other, score in result.elsewhere:
        print(f"  skipped {source}: looks like {other} ({score:.2f})")
    if result.refused_full:
        print(f"  {result.refused_full} not added: the limit is {store.max_faces} faces a person")
    print(f"look at them: vidette people sheet {person.id} /tmp/{person.id}-faces.jpg")
    return 0


# --- match, scan ---------------------------------------------------------------------------


def _cases(inputs: list[Path]) -> list[tuple[str, Case]]:
    from vidette.core.incidents import Incident

    def case(name: str, media: list[Path]) -> tuple[str, Case]:
        incident = Incident(source="cli", source_id=name, camera="camera", started_at=0.0)
        return name, Case(incident=incident, media=tuple(media))

    out: list[tuple[str, Case]] = []
    images: list[Path] = []
    for path in inputs:
        if path.is_dir():
            frames = sorted(p for p in path.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
            out.append(case(path.name, frames))
        elif path.suffix.lower() in VIDEO_SUFFIXES:
            out.append(case(path.name, [path]))
        elif path.suffix.lower() in IMAGE_SUFFIXES:
            images.append(path)
        else:
            raise StoreError(f"{path}: not a video, an image or a folder of images")
    if images:
        out.append(case(f"{len(images)} image(s)", images))
    return out


def _print_report(name: str, report: PeopleReport) -> None:
    print(
        f"{name}: {report.summary()}  ({report.frames_checked} frames, "
        f"{report.frames_with_readable_faces} with a readable face, "
        f"{report.unclear_frames} with only unclear faces)"
    )
    for seen in report.seen:
        who = seen.name if seen.status == "known" else "unknown"
        near = f", nearest {seen.closest}" if seen.closest else ""
        crop = f"  {seen.crop}" if seen.crop else ""
        print(
            f"  {seen.n}. {who} {seen.score:.2f}{near}; {seen.faces} face(s) in frames "
            f"{','.join(str(f + 1) for f in seen.frames)}; up to {seen.face_px} px{crop}"
        )


async def _match(
    config: PeopleConfig, inputs: list[Path], crops: Path | None, *, as_json: bool
) -> int:
    check = check_for(config, download=True, say=_say)
    if crops is not None:
        from vidette.paths import git_work_tree

        if git_work_tree(crops.resolve()) is not None:
            raise StoreError(f"{crops} is inside a git work tree; face crops must not be")
    results = {}
    for name, case in _cases(inputs):
        writer = None
        if crops is not None:
            writer = crop_writer(crops.resolve(), Path(name).stem)
        report = await check.check(case, writer)
        results[name] = report
        if not as_json:
            _print_report(name, report)
    if as_json:
        print(json.dumps({k: v.model_dump(mode="json") for k, v in results.items()}, indent=2))
    return 0


async def _scan(config: PeopleConfig, args: argparse.Namespace) -> int:
    root = Path(args.dataset).expanduser().resolve()
    clips = sorted(p for p in (root / "clips").glob("*") if p.suffix.lower() in VIDEO_SUFFIXES)
    if args.clip:
        clips = [p for p in clips if p.stem in set(args.clip)]
    if args.since:
        clips = [p for p in clips if p.stem >= args.since]

    if not args.all:
        clips = [p for p in clips if load_report(p) is None]
    if args.limit:
        clips = clips[: args.limit]
    if not clips:
        print("no clips to check (--all checks clips that have a report again)")
        return 0
    check = check_for(config, download=True, say=_say)
    print(f"checking {len(clips)} clip(s) against {len(check.store.people())} enrolled people")
    from vidette.collect.clips import load_meta

    counts = {"known": 0, "unknown": 0, "nothing": 0}
    for clip in clips:
        meta = load_meta(clip.with_suffix(".json"))
        media: list[Path] = [clip]
        if meta is not None:
            media += [root / frame for frame in meta.frames]
        from vidette.core.incidents import Incident

        case = Case(
            incident=Incident(
                source="dataset", source_id=clip.stem, camera="camera", started_at=0.0
            ),
            media=tuple(media),
        )
        report = await check.check(case, crops_for(clip))
        save_report(clip, report)
        if report.known:
            counts["known"] += 1
        elif report.unknown:
            counts["unknown"] += 1
        else:
            counts["nothing"] += 1
        _print_report(clip.stem, report)
    print(
        f"{len(clips)} clip(s): {counts['known']} with a known person, {counts['unknown']} "
        f"with unknown faces only, {counts['nothing']} without a readable face"
    )
    return 0


# --- confirm -----------------------------------------------------------------------------


def answer_word(text: str, store: PeopleStore) -> str | None:
    """A reply word → a person id, "stranger" or "unsure"; None when it is none of them."""
    word = " ".join(text.lower().replace("ё", "е").split())
    if word in STRANGER_WORDS:
        return STRANGER
    if word in UNSURE_WORDS:
        return UNSURE
    person = store.find(word)
    return None if person is None else person.id


def _confirm(config: PeopleConfig, clip: Path, face: int, answer: str) -> int:
    store = store_for(config)
    clip = clip.expanduser().resolve()
    report = load_report(clip)
    if report is None:
        print(f"error: {clip.name} has no face report yet: vidette people scan", file=sys.stderr)
        return 2
    word = answer_word(answer, store)
    if word is None:
        known = ", ".join(p.name for p in store.people())
        print(f"error: '{answer}' is not a person ({known}), stranger or unsure", file=sys.stderr)
        return 2
    engine: FaceEngine | None = None
    if word not in (STRANGER, UNSURE):
        engine = engine_for(config, download=True, say=_say)
    done = confirm(
        report,
        face,
        word,
        store=store,
        engine=engine,
        media_root=dataset_root(clip),
        where=f"clip:{clip.stem}",
    )
    save_report(clip, report)
    print(f"face {face} of {clip.stem}: {done.message}")
    return 0


# --- sheet -------------------------------------------------------------------------------


def _sheet(config: PeopleConfig, name: str, out: Path) -> int:
    """A grid of the reference crops with their numbers (needs OpenCV)."""
    store = store_for(config)
    person = store.require(name)
    from vidette.paths import git_work_tree

    if git_work_tree(out.expanduser().resolve().parent) is not None:
        raise StoreError(f"{out} is inside a git work tree; face images must not be")
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise EngineUnavailableError(
            "the sheet needs OpenCV: pip install 'vidette[people]'"
        ) from exc
    tiles = []
    for index, record in enumerate(store.faces(person.id), start=1):
        path = store.face_path(person.id, record.id)
        if path is None:
            continue
        img = cv2.imread(str(path))
        if img is None:
            continue
        img = cv2.resize(img, (160, 160))
        label = f"{index} {record.kind[0]}"
        cv2.putText(img, label, (4, 154), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3)
        cv2.putText(img, label, (4, 154), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        tiles.append(img)
    if not tiles:
        print(f"{person.name} has no faces yet")
        return 1
    per_row = 8
    blank = np.zeros_like(tiles[0])
    while len(tiles) % per_row:
        tiles.append(blank)
    rows = [np.hstack(tiles[i : i + per_row]) for i in range(0, len(tiles), per_row)]
    from vidette.paths import write_private

    ok, encoded = cv2.imencode(".jpg", np.vstack(rows), [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise StoreError("cannot encode the sheet")
    write_private(out.expanduser(), bytes(encoded.tobytes()))
    print(f"{out} ({len(store.faces(person.id))} faces; e = enrolled, c = confirmed)")
    return 0


# Imported for type checkers only: keeps the CLI's PeopleCheck name in one place.
__all__ = ["PeopleCheck", "add_arguments", "answer_word", "run"]
