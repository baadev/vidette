"""`vidette policy`: what a policy would have done, and what calibration changed.

    vidette policy dry-run [--days 7] [--policy-file FILE] [--calibration replay|active|off]
    vidette policy calibration [list]
    vidette policy calibration undo ID

The dry run reads the database read-only (a running `vidette serve` is not disturbed);
`calibration undo` writes the step back as `cli`. Docs: docs/calibration.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from vidette.calibration.dryrun import Mode, VerdictSource, render_json, render_text, replay
from vidette.core.config import ConfigError, VidetteConfig
from vidette.db import CalibrationRow, Database, read_alerted_events, read_calibration, read_events


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=None, help="the service's config")
    parser.add_argument(
        "--env-file",
        type=Path,
        action="append",
        default=[],
        help="KEY=VALUE file for the config's ${VAR}s (repeatable), as `vidette serve` reads",
    )
    commands = parser.add_subparsers(dest="policy_command", required=True)
    dry = commands.add_parser(
        "dry-run",
        help="replay the last days' stored verdicts through a policy and calibration; "
        "nothing is sent",
    )
    dry.add_argument("--days", type=float, default=7.0, help="how far back (default 7)")
    dry.add_argument(
        "--policy-file",
        type=Path,
        default=None,
        help="a YAML file with `policies:` (and optionally `calibration:`) to try instead "
        "of the config's",
    )
    dry.add_argument(
        "--calibration",
        choices=["replay", "active", "off"],
        default="replay",
        help="replay: from the marks as they were given (default); active: the steps in "
        "force now; off: the policy as written",
    )
    dry.add_argument(
        "--verdicts",
        choices=["live", "deep", "best"],
        default="live",
        help="which stored verdicts to route: the live judge's (default), deep analysis', "
        "or the live ones and deep analysis where the live judge gave none",
    )
    dry.add_argument("--db", type=Path, default=None, help="database (default: the config's)")
    dry.add_argument("--json", action="store_true")
    cal = commands.add_parser("calibration", help="list or take back calibration steps")
    cal.add_argument("action", nargs="?", choices=["list", "undo"], default="list")
    cal.add_argument("id", nargs="?", type=int, default=None, help="the step to take back")


def _config(args: argparse.Namespace) -> VidetteConfig:
    from vidette.vlm.setup import cli_config

    if args.env_file:
        from vidette.telegram.cli import load_env_files

        load_env_files(args.env_file)
    config, _source = cli_config(args.config)
    return config


def with_policy_file(config: VidetteConfig, path: Path) -> VidetteConfig:
    """The config with the policies (and calibration) of `path` instead of its own."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("policies"), list):
        raise ConfigError(f"{path}: expected a mapping with a `policies:` list")
    data: dict[str, Any] = config.model_dump(mode="python")
    data["policies"] = raw["policies"]
    if "calibration" in raw:
        data["calibration"] = raw["calibration"]
    try:
        merged = VidetteConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return merged


def cmd_dry_run(args: argparse.Namespace) -> int:
    try:
        config = _config(args)
        if args.policy_file is not None:
            config = with_policy_file(config, args.policy_file)
        db_path: Path = args.db or config.storage.database
        until = time.time()
        since = until - args.days * 86400
        lookback = since - config.calibration.window_s
        rows = read_events(db_path, since_ts=lookback)
        steps = read_calibration(db_path)
        alerted = read_alerted_events(db_path, since_ts=since - 3600)
    except (ConfigError, FileNotFoundError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    mode: Mode = args.calibration
    source: VerdictSource = args.verdicts
    run = replay(
        rows, config, since=since, until=until, mode=mode, steps=steps, alerted=alerted,
        verdicts=source,
    )  # fmt: skip
    if args.json:
        print(json.dumps(render_json(run), indent=2, ensure_ascii=False))
        return 0
    names = config.notifications.telegram.camera_names if config.notifications.telegram else {}
    print(render_text(run, camera_names=names), end="")
    return 0


def _line(row: CalibrationRow) -> str:
    if row.step_to >= 2:
        how = "digest only"
    elif row.step_to == 1:
        how = f"wakes only from {round((row.bar or 0) * 100)} % sure"
    else:
        how = "as written"
    state = "in force" if row.active else f"taken back {row.reverted_by}"
    reasons = row.evidence.get("reasons") or {}
    why = (
        f"{row.evidence.get('down')} x down "
        f"({', '.join(f'{k if k != "none" else "no reason"} {v}' for k, v in reasons.items())})"
        if "down" in row.evidence
        else "an up mark on one it had kept in the digest"
    )
    return (
        f"{row.id:>4}  {datetime.fromtimestamp(row.at):%Y-%m-%d %H:%M}  {row.policy}/"
        f"{row.scenario} on {row.camera}: {how}  [{state}]  {why}"
    )


async def _calibration(args: argparse.Namespace, config: VidetteConfig) -> int:
    from vidette.calibration.service import Calibrator

    db = Database(config.storage.database)
    await db.connect()
    try:
        if args.action == "undo":
            if args.id is None:
                print("error: name the step: vidette policy calibration undo ID", file=sys.stderr)
                return 2
            taken = await Calibrator(config, db).revert(args.id, "cli")
            if not taken:
                print(f"error: step {args.id} is not in force", file=sys.stderr)
                return 1
            for row in taken:
                print(f"taken back: {_line(row)}")
            return 0
        rows = await db.calibration_steps()
        if not rows:
            print("no calibration steps yet")
        for row in rows:
            print(_line(row))
        return 0
    finally:
        await db.close()


def run(args: argparse.Namespace) -> int:
    if args.policy_command == "dry-run":
        return cmd_dry_run(args)
    try:
        config = _config(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return asyncio.run(_calibration(args, config))
