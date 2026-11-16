"""`vidette deep status | show EVENT`: what deep analysis did, read from the server's
database (read-only; `vidette serve` does the work, docs/deep.md).

    vidette deep --config ~/.config/vidette/serve.yaml status
    vidette deep --config ~/.config/vidette/serve.yaml show 01a1e4…
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from vidette.core.config import ConfigError
from vidette.envfile import EnvFileError
from vidette.vlm.setup import cli_config


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=None,
                        help="the server's config (default $VIDETTE_CONFIG, then "
                        "~/.config/vidette/vidette.yaml)")  # fmt: skip
    parser.add_argument("--env-file", type=Path, action="append", default=[],
                        help="KEY=VALUE file for the config's ${VAR}s; repeatable")  # fmt: skip
    actions = parser.add_subparsers(dest="deep_command", required=True)
    status = actions.add_parser("status", help="the queue, today's count, the last clips")
    status.add_argument("--last", type=int, default=10, help="how many finished clips to list")
    show = actions.add_parser("show", help="one event's deep verdict as JSON")
    show.add_argument("event")


def _when(ts: float | None) -> str:
    return datetime.fromtimestamp(float(ts)).strftime("%d.%m %H:%M") if ts else "-"


def run(args: argparse.Namespace) -> int:
    try:
        if args.env_file:
            from vidette.telegram.cli import load_env_files

            load_env_files(args.env_file)
        config, _source = cli_config(args.config)
    except (ConfigError, EnvFileError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    database = config.storage.database
    if not database.is_file():
        print(f"error: no database at {database}", file=sys.stderr)
        return 2
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        try:
            if args.deep_command == "show":
                return _show(conn, args.event)
            return _status(conn, config.understanding.deep.max_clips_per_day, args.last)
        except sqlite3.OperationalError as exc:
            print(f"error: {exc} (has this server run deep analysis yet?)", file=sys.stderr)
            return 2


def _status(conn: sqlite3.Connection, cap: int, last: int) -> int:
    today = datetime.now().date().isoformat()
    states = dict(conn.execute("SELECT state, COUNT(*) FROM deep_jobs GROUP BY state").fetchall())
    used = conn.execute(
        "SELECT COUNT(*) FROM deep_jobs WHERE day = ? AND state IN ('done', 'failed')", (today,)
    ).fetchone()[0]
    print(f"queue: {json.dumps(states)}; today {used}/{cap}")
    rows = conn.execute(
        "SELECT j.*, e.deep FROM deep_jobs j LEFT JOIN events e ON e.id = j.event_id"
        " WHERE j.state IN ('done', 'failed') ORDER BY j.done_at DESC LIMIT ?",
        (last,),
    ).fetchall()
    for row in rows:
        stats: dict[str, Any] = json.loads(row["stats"] or "{}")
        deep: dict[str, Any] = json.loads(row["deep"] or "{}")
        first = (deep.get("verdicts") or [{}])[0]
        calls = ", ".join(f"{c['kind']} {c['s']:.0f}s" for c in stats.get("calls") or [])
        what = (
            f"{first.get('label')}/{first.get('level')} {deep.get('agreement')}"
            if row["state"] == "done"
            else f"failed: {row['error']}"
        )
        print(
            f"  {_when(row['done_at'])} {row['camera']:<11} {row['reason']:<12} {what}; "
            f"{stats.get('seconds', '-')} s ({calls}); slots {stats.get('slots', '?')}"
            + (f"; preempted {stats['preempted']}" if stats.get("preempted") else "")
        )
    head = conn.execute(
        "SELECT camera, reason, started_at FROM deep_jobs WHERE state = 'queued'"
        " ORDER BY priority, started_at DESC LIMIT 5"
    ).fetchall()
    for row in head:
        print(f"  next: {_when(row['started_at'])} {row['camera']} ({row['reason']})")
    return 0


def _show(conn: sqlite3.Connection, event: str) -> int:
    row = conn.execute("SELECT deep FROM events WHERE id = ?", (event,)).fetchone()
    if row is None or not row["deep"]:
        print(f"no deep verdict for {event}", file=sys.stderr)
        return 1
    print(json.dumps(json.loads(row["deep"]), ensure_ascii=False, indent=1))
    return 0
