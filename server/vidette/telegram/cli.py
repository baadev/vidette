"""`vidette telegram check | try`: is the bot set up, and one real alert end to end.

    vidette telegram check --config ~/.config/vidette/vidette.yaml --env-file …/telegram.env
    vidette telegram try ~/vidette-data/porch/clips/20260115-101500.mp4 --config … \
        --env-file …/telegram.env

`check` asks Telegram who the bot is and prints the chats and topics it will use (counts,
never ids or the token). `try` runs one clip through the live path (event row, face
check, the configured judge) and sends the result as a test alert with buttons to the
recipients' alerts topic, whatever the routing would have done; the note in the message
says where it would have gone. The buttons are answered by the running `vidette serve`,
which shares the database. Docs: docs/telegram.md.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from pathlib import Path
from typing import Any

from vidette.clipfolder import clip_incident
from vidette.collect.clips import load_meta
from vidette.core.config import ClipFolderConfig, ConfigError, VidetteConfig, VlmBackend
from vidette.core.events import InProcessEventBus
from vidette.core.incidents import Phase, as_payload, incident_topic
from vidette.db import Database
from vidette.envfile import EnvFileError, load_env_file
from vidette.events.intake import EventIntake
from vidette.people.engine import EngineUnavailableError
from vidette.people.models import ModelError
from vidette.people.setup import check_for
from vidette.people.store import StoreError
from vidette.telegram.api import TelegramApi, TelegramError
from vidette.telegram.service import TelegramService
from vidette.verdicts.vlm import VlmJudge
from vidette.verdicts.worker import VerdictWorker
from vidette.vlm.budget import MetaLedger
from vidette.vlm.client import VlmClient
from vidette.vlm.endpoint import BACKEND_META_KEY, effective_backend, endpoint_for, parse_choice
from vidette.vlm.setup import cli_config


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=None, help="vidette config (as serve)")
    parser.add_argument(
        "--env-file",
        type=Path,
        action="append",
        default=[],
        help="KEY=VALUE file with the token and other ${VAR}s (repeatable)",
    )
    actions = parser.add_subparsers(dest="telegram_command", required=True)
    actions.add_parser("check", help="ask Telegram who the bot is; print chats and topics")
    attempt = actions.add_parser(
        "try", help="judge one clip and send it as a test alert with buttons"
    )
    attempt.add_argument("clip", type=Path, help="a clip in a clip folder (or a dataset)")


def load_env_files(paths: list[Path]) -> None:
    for path in paths:
        applied, warnings = load_env_file(path.expanduser())
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
        if applied:
            print(f"env: {len(applied)} variable(s) from {path}")


def run(args: argparse.Namespace) -> int:
    try:
        load_env_files(args.env_file)
        config, source = cli_config(args.config)
    except (EnvFileError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if config.notifications.telegram is None:
        print(f"error: no notifications.telegram in {source or 'the config'}", file=sys.stderr)
        return 2
    if args.telegram_command == "check":
        return asyncio.run(_check(config))
    return asyncio.run(_try(config, args.clip.expanduser().resolve()))


async def _check(config: VidetteConfig) -> int:
    tg = config.notifications.telegram
    assert tg is not None
    api = TelegramApi(tg.token.get_secret_value(), base_url=tg.api_url)
    try:
        me = await api.get_me()
    except TelegramError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await api.aclose()
    print(f"bot      @{me.get('username')}")
    print(f"chats    {len(tg.allowed_chats)} allowed, {len(tg.recipient_chats)} get alerts "
          f"(recipients: {tg.recipients})")  # fmt: skip
    for number, chat in enumerate(tg.allowed_chats, start=1):
        topic = tg.topics.get(chat)
        topics = "no topics" if topic is None else (
            f"alerts topic {'set' if topic.alerts else 'missing'}, "
            f"digest topic {'set' if topic.digest else 'missing'}"
        )  # fmt: skip
        role = "gets alerts" if chat in tg.recipient_chats else "commands only"
        print(f"  chat {number}: {role}; {topics}")
    return 0


def _folder_for(config: VidetteConfig, clip: Path) -> ClipFolderConfig:
    for folder in config.clip_folders:
        if clip.is_relative_to(folder.path.expanduser().resolve()):
            return folder.model_copy(update={"path": folder.path.expanduser().resolve()})
    root = clip.parent.parent if clip.parent.name == "clips" else clip.parent
    meta = load_meta(clip.with_suffix(".json"))
    return ClipFolderConfig(path=root, camera=meta.camera if meta else "camera")


async def _noop(_kind: str, _payload: dict[str, Any]) -> None:
    return None


async def _try(config: VidetteConfig, clip: Path) -> int:
    if not clip.is_file():
        print(f"error: {clip} does not exist", file=sys.stderr)
        return 2
    folder = _folder_for(config, clip)
    incident = clip_incident(folder, clip)
    db = Database(config.storage.database)
    await db.connect()
    bus = InProcessEventBus()
    service: TelegramService | None = None
    clients: list[VlmClient] = []
    try:
        intake = EventIntake(db, bus, media_dir=config.storage.media_dir)
        row = await intake.handle(incident_topic(Phase.closed), as_payload(incident))
        if row is None:
            print("error: the clip did not become an event (see the log)", file=sys.stderr)
            return 1
        await db.update_event(row.id, state="observed")  # judged again on every try
        print(f"event    {row.id} ({incident.camera}, {clip.name})")

        people = None
        store = engine = None
        if config.people.enabled:
            try:
                people = check_for(config.people, download=False, say=lambda _l: None)
                store, engine = people.store, people.engine
            except (StoreError, ModelError, EngineUnavailableError) as exc:
                print(f"people   off for this try ({exc})")
        stored = parse_choice(await db.get_meta(BACKEND_META_KEY))
        backend = effective_backend(config, stored)

        async def resolver() -> VlmBackend:
            return backend

        def judges(chosen: VlmBackend) -> VlmJudge:
            client = VlmClient(endpoint_for(config, chosen), ledger=MetaLedger(db))
            clients.append(client)
            vlm = config.understanding.vlm
            return VlmJudge(client, frames_per_call=vlm.frames_per_call, language=vlm.language)

        if backend is not VlmBackend.none:
            endpoint = endpoint_for(config, backend)
            print(f"judge    {backend.value}: {endpoint.host} (timeout {endpoint.timeout_s:.0f} s)")
        worker = VerdictWorker(
            config, bus, db, emit=_noop, backend=resolver, judges=judges,
            media_dir=config.storage.media_dir, max_age_s=math.inf, people=people,
        )  # fmt: skip
        judged = await worker.handle(incident)
        row = judged or await db.get_event(row.id)
        assert row is not None
        print(f"verdict  {row.state}: {row.summary or 'no summary'}")
        service = TelegramService(
            config, db, bus, emit=_noop, people_store=store, face_engine=engine
        )
        sent = await service.notify_event(row, test=True)
        if not sent:
            print("error: nothing was sent (see telegram errors above)", file=sys.stderr)
            if service.status.last_error:
                print(f"  {service.status.last_error}", file=sys.stderr)
            return 1
        print(f"sent     {len(sent)} test alert(s) with buttons; `vidette serve` answers them")
        return 0
    finally:
        for client in clients:
            await client.aclose()
        if service is not None:
            await service.api.aclose()
        await db.close()
