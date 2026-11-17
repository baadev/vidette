"""`vidette policy`: what calibration changed, and taking it back.

    vidette policy calibration [list]
    vidette policy calibration undo ID

`calibration undo` writes the step back as `cli`. Docs: docs/calibration.md.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from vidette.core.config import ConfigError, VidetteConfig
from vidette.db import CalibrationRow, Database


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
    try:
        config = _config(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return asyncio.run(_calibration(args, config))
