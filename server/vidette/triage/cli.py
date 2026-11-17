"""`vidette triage`: the pre-filter in front of the live judge (docs/prefilter.md).

    vidette triage models                       download the detector model (pinned)
    vidette triage check CLIP...                now / later / skip for clips, with why
    vidette triage replay [--days 3] [--sample 0.25] [--cache FILE]
                                                the pre-filter over stored events: classes
                                                per day, what it would have skipped of
                                                what is known, the judge's queue simulated

Nothing here calls a model or writes to the service's database (it is opened
read-only). `models` is the only command that downloads anything.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from vidette.core.config import ConfigError, VidetteConfig
from vidette.db import EventRow, read_events
from vidette.triage.detector import (
    YOLOX_S,
    DetectorUnavailableError,
    ObjectDetector,
    load_detector,
)
from vidette.triage.observe import observe
from vidette.triage.replay import (
    Check,
    DaySimulation,
    Replayed,
    case_for_event,
    checks,
    classify_all,
    gather,
    measured_durations,
    per_day,
    per_reason,
    pick,
    simulate_days,
    stratified_weights,
)
from vidette.triage.rules import RANK_LATER, RANK_PEOPLE, RANK_SECOND, classify

DEFAULT_CACHE = Path("~/.cache/vidette/triage-replay.jsonl")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=None, help="the service's config")
    parser.add_argument(
        "--env-file",
        type=Path,
        action="append",
        default=[],
        help="KEY=VALUE file for the config's ${VAR}s (repeatable), as `vidette serve` reads",
    )
    commands = parser.add_subparsers(dest="triage_command", required=True)
    commands.add_parser(
        "models",
        help=f"download the detector model ({YOLOX_S.what}, {YOLOX_S.license}, "
        f"{YOLOX_S.size / 1e6:.1f} MB) into the model cache",
    )
    check = commands.add_parser("check", help="now / later / skip for clips, with why")
    check.add_argument("clips", type=Path, nargs="+", help="clips (a clip folder's .mp4)")
    check.add_argument("--json", action="store_true")
    replay = commands.add_parser(
        "replay",
        help="the pre-filter over the events a service already has; nothing is sent, no "
        "model is called",
    )
    replay.add_argument("--days", type=float, default=3.0, help="how far back (default 3)")
    replay.add_argument(
        "--sample",
        type=float,
        default=0.25,
        help="share of the events without evidence to look at (default 0.25; 1: all). "
        "Every event with an alert, a mark, a face, a person or a waking verdict is "
        "looked at",
    )
    replay.add_argument(
        "--cache",
        type=Path,
        default=DEFAULT_CACHE,
        help=f"observations, one line per event, reused next time (default {DEFAULT_CACHE})",
    )
    replay.add_argument("--jobs", type=int, default=2, help="clips looked at in parallel")
    replay.add_argument(
        "--max-new",
        type=int,
        default=None,
        help="look at no more than this many clips not in the cache yet (the report "
        "weights what was looked at)",
    )
    replay.add_argument("--db", type=Path, default=None, help="database (default: the config's)")
    replay.add_argument("--json", action="store_true")


def _config(args: argparse.Namespace) -> VidetteConfig:
    from vidette.vlm.setup import cli_config

    if args.env_file:
        from vidette.telegram.cli import load_env_files

        load_env_files(args.env_file)
    config, _source = cli_config(args.config)
    return config


def _models(config: VidetteConfig) -> int:
    folder = config.understanding.triage.models_dir or config.people.models_dir
    try:
        detector = load_detector(folder, download=True)
    except DetectorUnavailableError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"ready: {detector.name} ({YOLOX_S.license}), checked against its pinned sha256")
    return 0


async def _check(config: VidetteConfig, clips: list[Path], as_json: bool) -> int:
    from vidette.clipfolder import clip_incident
    from vidette.core.config import ClipFolderConfig
    from vidette.verdicts.base import Case

    settings = config.understanding.triage
    try:
        detector = load_detector(
            settings.models_dir or config.people.models_dir, threads=settings.threads
        )
    except DetectorUnavailableError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    status = 0
    for clip in clips:
        if not clip.is_file():
            print(f"{clip}: not a file", file=sys.stderr)
            status = 1
            continue
        root = clip.resolve().parent.parent
        folder = ClipFolderConfig(path=root, camera="camera")
        incident = clip_incident(folder, clip.resolve())
        case = Case(incident=incident, media=tuple(Path(m) for m in incident.media))
        observation = await observe(
            case, detector, frames=settings.frames, key_frames=settings.key_frames
        )
        decision = classify(observation, settings)
        if as_json:
            print(json.dumps({"clip": str(clip), **decision.record(observation, phase="final")}))
            continue
        facts = decision.facts
        print(
            f"{clip.name}  {decision.klass.value:<5}  {decision.why}  · person "
            f"{facts.person:.2f}, moving vehicle {facts.vehicle_moving:.2f}, animal "
            f"{facts.animal:.2f}, {facts.frames} frames, changed "
            f"{(observation.activity_peak or 0) * 100:.1f} %, light "
            f"{(observation.light or 0) * 100:.1f} %, {observation.seconds:.1f} s"
            + (f"  ({observation.error})" if observation.error else "")
        )
    return status


def _check_lines(table: list[Check]) -> list[str]:
    lines = [
        f"{'what is known':<52}{'events':>7}{'later':>7}{'skipped':>9}",
    ]
    skipped: list[tuple[Check, Replayed]] = []
    for check in table:
        mark = "  must be 0" if check.must_be_zero else ""
        lines.append(
            f"{check.name:<52}{check.total:>7}{check.later:>7}{len(check.skipped):>9}{mark}"
        )
        skipped += [(check, item) for item in check.skipped]
    shown: set[str] = set()
    for check, item in skipped:
        if not check.must_be_zero or item.event_id in shown:
            continue
        shown.add(item.event_id)
        lines.append(f"  skipped {_item(item)}")
    return lines


def _item(item: Replayed) -> str:
    ev = item.evidence
    said = ""
    if ev is not None:
        said = f"; live: {ev.live_label or '-'} ({ev.live_persons} people)"
        if ev.deep_label:
            said += f", deep: {ev.deep_label} ({ev.deep_persons} people)"
        if ev.early_only:
            said += ", judged from its early window"
    when = datetime.fromtimestamp(item.at).strftime("%m-%d %H:%M:%S")
    return f"{item.event_id} {item.camera} {when}: {item.why}{said}"


def _sim_lines(days: list[DaySimulation], durations: list[float]) -> list[str]:
    typical = statistics.median(durations) if durations else 45.0
    lines = [
        f"The live judge's queue, simulated: one slot, the last day's {len(durations)} "
        f"measured verdicts (median {typical:.0f} s) taken in turn, deep analysis' daytime "
        "turns included.",
        f"{'day':<12}{'clips':>7}{'judged then':>13}{'simulated':>11}{'with pre-filter':>17}"
        f"{'people':>9}{'maybe/car':>11}{'later':>8}{'skipped':>9}",
    ]
    for day in days:
        before, after = day.before, day.after
        ranks = [
            _pct(after.by_rank.get(r, 0), after.offered_rank.get(r, 0))
            for r in (RANK_PEOPLE, RANK_SECOND, RANK_LATER)
        ]
        lines.append(
            f"{day.day:<12}{day.events:>7}{_pct(day.actual_judged, day.events):>13}"
            f"{_pct(before.judged_final, before.finals):>11}"
            f"{_pct(after.judged_final, after.finals):>17}"
            f"{ranks[0]:>9}{ranks[1]:>11}{ranks[2]:>8}"
            f"{_pct(after.offered.get('skip', 0), after.finals):>9}"
        )
    lines.append(
        "judged: whole clips the judge reached (early windows not counted); `judged then` "
        "is what the service really did, `simulated` the same queue modelled, to show how "
        "far the model is off; people / maybe/car (a weaker person candidate or a moving "
        "vehicle) / later: the share of each the judge would reach with the pre-filter; "
        "skipped: the share of all clips."
    )
    return lines


def _pct(part: int, whole: int) -> str:
    return f"{part * 100 / whole:.0f} % " if whole else "-  "


async def _replay(args: argparse.Namespace, config: VidetteConfig) -> int:
    settings = config.understanding.triage
    db_path: Path = args.db or config.storage.database
    until = time.time()
    since = until - args.days * 86400
    try:
        rows = read_events(db_path, since_ts=since)
    except (FileNotFoundError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    picked, _weights = pick(rows, sample=args.sample)
    try:
        detectors: list[ObjectDetector] = [
            load_detector(settings.models_dir or config.people.models_dir, threads=2)
            for _ in range(max(1, args.jobs))
        ]
    except DetectorUnavailableError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    say = (lambda _line: None) if args.json else (lambda line: print(line, flush=True))
    observations = await gather(
        _evidence_first(picked),
        config,
        detectors,
        cache=args.cache.expanduser(),
        frames=settings.frames,
        key_frames=settings.key_frames,
        jobs=args.jobs,
        max_new=args.max_new,
        say=say,
    )
    known = {row.id: row for row in rows}
    looked = {k: v for k, v in observations.items() if k in known}
    weights = stratified_weights(rows, looked)
    replayed = classify_all(
        rows, looked, settings, weights, looked_share=len(looked) / max(1, len(rows))
    )
    table = checks(replayed, known, config)
    durations = measured_durations(r for r in rows if r.started_at >= until - 86400)
    if len(durations) < 50:
        durations = measured_durations(rows)
    deep_seconds = [
        float(r.deep["seconds"])
        for r in rows
        if isinstance(r.deep, dict) and isinstance(r.deep.get("seconds"), int | float)
    ]
    turn = statistics.median(deep_seconds) if deep_seconds else 120.0
    days = simulate_days(rows, replayed, config, durations=durations, turn_s=turn)
    if args.json:
        print(json.dumps(_as_json(replayed, table, days), indent=2, ensure_ascii=False))
        return 0
    print(
        f"Pre-filter replay: {datetime.fromtimestamp(since):%Y-%m-%d %H:%M} to "
        f"{datetime.fromtimestamp(until):%Y-%m-%d %H:%M}, {len(rows)} events, "
        f"{len(replayed)} looked at (every event with evidence, a {args.sample:.0%} sample "
        "of the rest, weighted per day and camera). No model was called; nothing was sent."
    )
    print()
    print(f"{'day':<12}{'events':>8}{'now':>7}{'later':>7}{'skip':>7}   (estimated)")
    for day, row in per_day(replayed).items():
        print(f"{day:<12}{row['events']:>8}{row['now']:>7}{row['later']:>7}{row['skip']:>7}")
    print()
    for why, count in per_reason(replayed).most_common():
        print(f"{count:>8}  {why}")
    print()
    for line in _check_lines(table):
        print(line)
    print()
    for line in _sim_lines(days, durations):
        print(line)
    return 0


def _evidence_first(rows: list[EventRow]) -> list[EventRow]:
    """Every event with evidence first (newest first), then the sample in a fixed random
    order: a run stopped half way is still a random subset of the rest."""
    import random

    from vidette.triage.replay import evidence_of

    first = sorted((r for r in rows if evidence_of(r)), key=lambda r: -r.started_at)
    rest = [r for r in rows if not evidence_of(r)]
    random.Random(3).shuffle(rest)
    return first + rest


def _as_json(
    replayed: list[Replayed], table: list[Check], days: list[DaySimulation]
) -> dict[str, Any]:
    return {
        "days": {day: dict(row) for day, row in per_day(replayed).items()},
        "reasons": dict(per_reason(replayed)),
        "checks": [
            {
                "name": c.name,
                "must_be_zero": c.must_be_zero,
                "events": c.total,
                "later": c.later,
                "skipped": [s.event_id for s in c.skipped],
            }
            for c in table
        ],
        "simulation": [
            {
                "day": d.day,
                "events": d.events,
                "judged_then": d.actual_judged,
                "simulated_before": d.before.judged_final,
                "with_prefilter": d.after.judged_final,
                "now_offered": d.after.offered.get("now", 0),
                "now_judged": d.after.by_class.get("now", 0),
                "later_offered": d.after.offered.get("later", 0),
                "later_judged": d.after.by_class.get("later", 0),
                "skipped": d.after.offered.get("skip", 0),
            }
            for d in days
        ],
    }


def run(args: argparse.Namespace) -> int:
    try:
        config = _config(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.triage_command == "models":
        return _models(config)
    if args.triage_command == "check":
        return asyncio.run(_check(config, args.clips, args.json))
    return asyncio.run(_replay(args, config))


__all__ = ["add_arguments", "case_for_event", "run"]
