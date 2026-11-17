"""CLI: `vidette serve | validate | frigate | eval | collect | label | people | telegram | deep`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vidette import __version__


def _cmd_validate(path: Path) -> int:
    from vidette.core.config import validate_config_text

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        return 2
    report = validate_config_text(text)
    if not report.valid:
        print(f"✗ {path} is invalid:")
        for error in report.errors:
            print(f"  - {error}")
        return 1
    print(f"✓ {path} is valid")
    for warning in report.warnings:
        print(f"  ⚠ {warning}")
    return 0


def _cmd_serve(host: str, port: int, config: Path | None, env_files: list[Path]) -> int:
    import os

    import uvicorn

    from vidette.envfile import EnvFileError
    from vidette.telegram.cli import load_env_files

    try:
        load_env_files(env_files)
    except EnvFileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if config is not None:
        os.environ["VIDETTE_CONFIG"] = str(config.expanduser())
    config_path = os.environ.get("VIDETTE_CONFIG", "/config/vidette.yaml")
    print(f"vidette v{__version__} · http://{host}:{port} · config: {config_path}")
    import logging

    # the workers' own lines (deep analysis, the merge pass, a judge that abstained) next
    # to uvicorn's; other libraries stay at warnings
    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("vidette").setLevel(logging.INFO)
    uvicorn.run("vidette.api.app:create_app", factory=True, host=host, port=port)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vidette",
        description="Alert policies in plain language, judged on top of Frigate.",
    )
    parser.add_argument("--version", action="version", version=f"vidette {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="run the API + web app")
    serve.add_argument("--host", default="0.0.0.0")  # container default
    serve.add_argument("--port", type=int, default=8642)
    serve.add_argument("--config", type=Path, default=None, help="config file path")
    serve.add_argument(
        "--env-file",
        type=Path,
        action="append",
        default=[],
        help="KEY=VALUE file for ${VAR}s in the config, such as a bot token (repeatable; "
        "variables already set win)",
    )

    validate = subparsers.add_parser("validate", help="validate a config file")
    validate.add_argument("config", type=Path)

    frigate = subparsers.add_parser("frigate", help="inspect a Frigate install (docs/frigate.md)")
    frigate_cmds = frigate.add_subparsers(dest="frigate_command", required=True)
    check = frigate_cmds.add_parser("check", help="ask Frigate's API for version and cameras")
    check.add_argument("--config", type=Path, default=None, help="vidette config with frigate:")
    check.add_argument("--url", default=None, help="Frigate API base, e.g. http://frigate:5000")
    tail = frigate_cmds.add_parser("tail", help="print incidents live from Frigate's MQTT")
    tail.add_argument("--config", type=Path, required=True, help="vidette config with frigate:")
    tail.add_argument("--record", type=Path, default=None, help="append raw messages (JSONL)")
    replay = frigate_cmds.add_parser("replay", help="print incidents from a recorded session")
    replay.add_argument("recording", type=Path, help="JSONL written by `tail --record`")
    replay.add_argument("--config", type=Path, default=None)
    replay.add_argument("--prefix", default="frigate", help="Frigate's mqtt.topic_prefix")

    evaluate = subparsers.add_parser(
        "eval",
        help="score policies on a labelled clip set (docs/eval.md)",
        description="Score judges against a labelled dataset: precision, recall, false "
        "alarms per day and VLM cost. Without --judge, every built-in baseline runs.",
    )
    evaluate.add_argument(
        "dataset",
        type=Path,
        nargs="+",
        help="dataset dir (or its labels.yaml); several are scored as one (docs/eval.md)",
    )
    evaluate.add_argument(
        "--policy", action="append", default=None, help="policy name (repeatable; default all)"
    )
    evaluate.add_argument(
        "--judge",
        action="append",
        default=None,
        help="always | never | frigate-severity | frigate-genai | vlm-local | vlm-cloud | "
        "stored-live | stored-deep | stored-best | triage | triage-now (repeatable; default: "
        "the four baselines; VLM judges run only when named; stored-* read what a service "
        "already said; triage* is the pre-filter, docs/prefilter.md)",
    )
    evaluate.add_argument(
        "--policy-from-config",
        action="append",
        default=None,
        metavar="NAME",
        help="score a policy of the config (--config): its alerting levels' scenarios are "
        "the ground truth (repeatable)",
    )
    evaluate.add_argument(
        "--db",
        type=Path,
        default=None,
        help="the service's database for stored-* judges (default: the config's), read-only",
    )
    evaluate.add_argument(
        "--config",
        type=Path,
        default=None,
        help="config with understanding.vlm (default $VIDETTE_CONFIG, then "
        "~/.config/vidette/vidette.yaml)",
    )
    evaluate.add_argument(
        "--include-drafts",
        action="store_true",
        help="score machine-drafted labels nobody reviewed yet (the report says so)",
    )
    evaluate.add_argument("--json", action="store_true", help="machine-readable output")
    evaluate.add_argument(
        "--people",
        action="store_true",
        help="run the local face check on every entry and tell VLM judges who was "
        "recognized (docs/people.md)",
    )
    evaluate.add_argument(
        "--known-people",
        choices=["judge", "quiet"],
        default=None,
        help="override the policies' known_people for this run (with --people)",
    )
    evaluate.add_argument("--usd-per-call", type=float, default=0.0, help="VLM price per call")
    evaluate.add_argument("--usd-per-image", type=float, default=0.0, help="VLM price per image")
    evaluate.add_argument(
        "--frames-per-call", type=int, default=4, help="frames per VLM call in the projection"
    )
    evaluate.add_argument(
        "--scaffold",
        action="store_true",
        help="print labels.yaml entries for clips that have none, then exit",
    )

    from vidette.collect import cli as collect_cli

    collect = subparsers.add_parser(
        "collect",
        help="record a wake-on-motion camera's clips into a dataset (docs/collect.md)",
        description="Wait for a battery camera's RTSP stream (Eufy NAS RTSP and the like), "
        "copy each wake period to DATASET/clips/<YYYYMMDD-HHMMSS>.mp4 with key frames and a "
        "JSON sidecar, and stop at hard disk limits. The camera URL comes from "
        "VIDETTE_RTSP_URL or ~/.config/vidette/rtsp-url, never from the command line.",
    )
    collect_cli.add_arguments(collect)

    from vidette.label import cli as label_cli

    label = subparsers.add_parser(
        "label",
        help="draft labels with a VLM, review them on contact sheets (docs/label.md)",
    )
    label_cli.add_arguments(label)

    from vidette.people import cli as people_cli

    people = subparsers.add_parser(
        "people",
        help="known people: enroll faces, check clips, confirm (docs/people.md)",
        description="Local face recognition for people who agreed to be enrolled. The "
        "people folder holds biometric data: outside any git repository, owner-only.",
    )
    people_cli.add_arguments(people)

    from vidette.telegram import cli as telegram_cli

    telegram = subparsers.add_parser(
        "telegram",
        help="the Telegram bot: check the setup, send a test alert (docs/telegram.md)",
    )
    telegram_cli.add_arguments(telegram)

    from vidette.calibration import cli as policy_cli

    policy = subparsers.add_parser(
        "policy",
        help="dry-run a policy over stored verdicts; list or take back calibration "
        "(docs/calibration.md)",
    )
    policy_cli.add_arguments(policy)

    from vidette.deep import cli as deep_cli

    deep = subparsers.add_parser(
        "deep",
        help="deep analysis: the queue and the verdicts vidette serve made (docs/deep.md)",
    )
    deep_cli.add_arguments(deep)

    from vidette.triage import cli as triage_cli

    triage = subparsers.add_parser(
        "triage",
        help="the pre-filter in front of the live judge: model, check clips, replay "
        "(docs/prefilter.md)",
    )
    triage_cli.add_arguments(triage)

    args = parser.parse_args(argv)
    if args.command == "triage":
        return triage_cli.run(args)
    if args.command == "policy":
        return policy_cli.run(args)
    if args.command == "deep":
        return deep_cli.run(args)
    if args.command == "telegram":
        return telegram_cli.run(args)
    if args.command == "collect":
        return collect_cli.run(args)
    if args.command == "label":
        return label_cli.run(args)
    if args.command == "people":
        return people_cli.run(args)
    if args.command == "eval":
        from vidette.eval import cli as eval_cli
        from vidette.eval.metrics import CostModel

        if args.scaffold:
            return max(eval_cli.cmd_scaffold(path) for path in args.dataset)
        return eval_cli.cmd_eval(
            args.dataset,
            policies=args.policy,
            judges=args.judge,
            as_json=args.json,
            config_path=args.config,
            include_drafts=args.include_drafts,
            people=args.people,
            known_people=args.known_people,
            from_config=args.policy_from_config,
            database=args.db,
            cost=CostModel(
                usd_per_call=args.usd_per_call,
                usd_per_image=args.usd_per_image,
                frames_per_call=args.frames_per_call,
            ),
        )
    if args.command == "validate":
        return _cmd_validate(args.config)
    if args.command == "frigate":
        from vidette.frigate import cli as frigate_cli

        if args.frigate_command == "check":
            return frigate_cli.cmd_check(args.config, args.url)
        if args.frigate_command == "tail":
            return frigate_cli.cmd_tail(args.config, args.record)
        return frigate_cli.cmd_replay(args.recording, args.config, args.prefix)
    return _cmd_serve(args.host, args.port, args.config, args.env_file)


if __name__ == "__main__":
    raise SystemExit(main())
