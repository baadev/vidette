"""Application runtime: owns and wires every subsystem.

Boot sequence (FastAPI lifespan calls start/stop):
1. Database.connect (migrations).
2. With `workers=True`: NotificationDispatcher and MqttPublisher (both no-ops without
   configured rules / broker), the EventIntake (incidents → event rows), the verdict
   worker, the Telegram bot when `notifications.telegram` is set (ADR-0013), then the
   Frigate source when `frigate:` is configured and the clip folders (ADR-0010).
   Frigate or its broker being unreachable never blocks boot: the source retries with
   backoff and reports through system events and /api/v1/system.
3. stop() reverses in order; every subsystem stop is awaited and exception-contained.

Since v0.2 (ADR-0009) Vidette opens no camera connection, records nothing and runs no
detector: incidents arrive from Frigate. The v0.1 recorder, gateway manager, keep-warm
holders and cascade pipeline are frozen at tag `nvr-core`.

System events flow two ways on purpose: persisted via Database.add_system_event (the
audit trail the API serves) and published on the in-process bus (what the notification
dispatcher and the WebSocket feed consume). `emit()` is the single helper that does both.

Environment (documented in docs/configuration.md):
  VIDETTE_CONFIG       config file path         (default /config/vidette.yaml)
  VIDETTE_WEB_DIST     built web app dir

A missing config file is not an error: it boots with VidetteConfig() defaults so the
first-run wizard has an API to talk to (wizard-mode).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from vidette.auth.service import AuthService
from vidette.clipfolder import ClipFolderSource
from vidette.core.config import VidetteConfig, VlmBackend, load_config
from vidette.core.events import InProcessEventBus
from vidette.db import Database
from vidette.events.intake import EventIntake
from vidette.frigate.client import FrigateClient
from vidette.frigate.source import FrigateSource
from vidette.notify.apprise_channel import AppriseNotifier
from vidette.notify.dispatcher import NotificationDispatcher
from vidette.notify.mqtt import MqttPublisher
from vidette.notify.webhook import WebhookNotifier
from vidette.notify.webpush import WebPushNotifier
from vidette.people.check import CheckSettings, PeopleCheck
from vidette.people.engine import EngineUnavailableError, FaceEngine
from vidette.people.identities import IdentityError, IdentityStore
from vidette.people.models import ModelError
from vidette.people.recurrence import with_recurrence
from vidette.people.setup import engine_for, identities_for, store_for
from vidette.people.store import PeopleStore, StoreError
from vidette.telegram.service import TelegramService
from vidette.verdicts.base import Judge
from vidette.verdicts.vlm import VlmJudge
from vidette.verdicts.worker import VerdictWorker
from vidette.vlm.budget import MetaLedger
from vidette.vlm.client import VlmClient
from vidette.vlm.endpoint import BACKEND_META_KEY, effective_backend, endpoint_for, parse_choice

logger = logging.getLogger(__name__)


def default_config_path() -> Path:
    return Path(os.environ.get("VIDETTE_CONFIG", "/config/vidette.yaml"))


class AppRuntime:
    def __init__(self, config: VidetteConfig, *, config_warnings: list[str] | None = None) -> None:
        self.config = config
        self.config_warnings = list(config_warnings or [])
        self.db = Database(config.storage.database)
        self.auth = AuthService(self.db, config.server.auth.mode)
        self.bus = InProcessEventBus()
        self.notifier = NotificationDispatcher(
            config,
            self.bus,
            emit=self.emit,
            base_url=config.server.base_url,
            notifiers={
                "webhook": WebhookNotifier(),
                "apprise": AppriseNotifier(),
                "webpush": WebPushNotifier(self.db),
            },
        )
        self.mqtt = MqttPublisher(config, self.bus, emit=self.emit)
        self.frigate_source: FrigateSource | None = None
        self.frigate_client: FrigateClient | None = None
        if config.frigate is not None:
            self.frigate_source = FrigateSource(config.frigate, self.bus, emit=self.emit)
            self.frigate_client = FrigateClient(config.frigate.url)
        self.clip_folders: ClipFolderSource | None = None
        if config.clip_folders:
            self.clip_folders = ClipFolderSource(
                config.clip_folders, self.bus, self.db, emit=self.emit
            )
        self.intake = EventIntake(
            self.db,
            self.bus,
            media_dir=config.storage.media_dir,
            snapshots=self.frigate_client,
        )
        self.vlm_ledger = MetaLedger(self.db)
        self._vlm_clients: list[VlmClient] = []
        self.people_store: PeopleStore | None = None
        self.face_engine: FaceEngine | None = None
        self.people_check: PeopleCheck | None = None
        self.people_error: str | None = None
        self.identities: IdentityStore | None = None  # people.track_unknown (ADR-0014)
        if config.people.enabled:
            self._setup_people()
        self.verdicts = VerdictWorker(
            config,
            self.bus,
            self.db,
            emit=self.emit,
            backend=self.vlm_backend,
            judges=lambda backend: with_recurrence(self._make_judge(backend), self.config),
            frames=self.frigate_client,
            media_dir=config.storage.media_dir,
            people=self.people_check,
        )
        self.telegram: TelegramService | None = None
        telegram = config.notifications.telegram
        if telegram is not None and telegram.enabled:
            self.telegram = TelegramService(
                config,
                self.db,
                self.bus,
                emit=self.emit,
                people_store=self.people_store,
                face_engine=self.face_engine,
                identities=self.identities,
                judge_status=self.judge_line,
            )
        self._workers_started = False

    async def judge_line(self) -> str:
        """The judge in one line for people (the bot's /status)."""
        backend = await self.vlm_backend()
        if backend is VlmBackend.none:
            return "выключен (backend: none)"
        endpoint = endpoint_for(self.config, backend)
        status = self.verdicts.status
        model = endpoint.model.rstrip("/").rsplit("/", 1)[-1]
        line = (
            f"{backend.value} {model}; с запуска {status.judged}, "
            f"пропущено старых {status.skipped_old}"
        )
        if status.last_error:
            line += f"; ошибка: {status.last_error[:80]}"
        return line

    # --- known people --------------------------------------------------------------------

    def _setup_people(self) -> None:
        """The store and the engine; never downloads (`vidette people models` does).
        Anything missing becomes a config warning and the People page says why."""
        people = self.config.people
        try:
            self.people_store = store_for(people)
        except StoreError as exc:
            self.people_error = str(exc)
        else:
            try:
                self.face_engine = engine_for(people, download=False)
            except (ModelError, EngineUnavailableError) as exc:
                self.people_error = str(exc)
            else:
                if people.track_unknown:
                    try:
                        self.identities = identities_for(people)
                    except IdentityError as exc:
                        self.config_warnings.append(f"people.identities: {exc}")
                self.people_check = PeopleCheck(
                    self.people_store,
                    self.face_engine,
                    CheckSettings(
                        fps=people.frames_per_second,
                        max_frames=people.max_frames,
                        extra_fps=people.extra_fps,
                        extra_frames=people.extra_frames,
                    ),
                    self.identities,
                )
        if self.people_error is not None:
            self.config_warnings.append(f"people: {self.people_error}")

    # --- the VLM judge -----------------------------------------------------------------

    async def vlm_backend(self) -> VlmBackend:
        """The System page's stored choice if still available, else the config's."""
        stored = parse_choice(await self.db.get_meta(BACKEND_META_KEY))
        return effective_backend(self.config, stored)

    def _make_judge(self, backend: VlmBackend) -> Judge:
        client = VlmClient(endpoint_for(self.config, backend), ledger=self.vlm_ledger)
        self._vlm_clients.append(client)
        vlm = self.config.understanding.vlm
        return VlmJudge(client=client, frames_per_call=vlm.frames_per_call, language=vlm.language)

    @classmethod
    def from_environment(cls) -> AppRuntime:
        path = default_config_path()
        if path.exists():
            config, warnings = load_config(path)
        else:
            config = VidetteConfig()
            warnings = [
                f"config file {path} not found, starting with defaults; "
                "create it from deploy/config.example.yaml"
            ]
        return cls(config, config_warnings=warnings)

    # --- system events -------------------------------------------------------------------

    @staticmethod
    def _bus_topic(kind: str) -> str | None:
        """Map a system-event kind to its bus topic.

        Everything operational lives under `system.*` so the documented notification rule
        patterns match (`frigate.unavailable` → `system.frigate.unavailable`).
        Delivery-failure kinds (`notify.*`) are persisted but never published: a failing
        webhook matched by a `system.*` rule would otherwise notify about its own failure,
        forever.
        """
        if kind.startswith("notify."):
            return None
        if kind.startswith(("event.", "system.")):
            return kind
        return f"system.{kind}"

    async def emit(self, kind: str, payload: dict[str, Any]) -> None:
        """Persist a system event AND publish it on the bus (notifications, live feeds)."""
        try:
            await self.db.add_system_event(kind, payload)
        except Exception:
            logger.exception("failed to persist system event %s", kind)
        topic = self._bus_topic(kind)
        if topic is not None:
            await self.bus.publish(topic, payload)

    # --- lifecycle -------------------------------------------------------------------------

    async def start(self, *, workers: bool = True) -> None:
        await self.db.connect()
        if not workers:
            return
        try:
            self.config.storage.media_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            detail = (
                f"media dir {self.config.storage.media_dir} is not writable: {exc} — "
                "event snapshots cannot be cached until it is fixed"
            )
            logger.error(detail)
            self.config_warnings.append(detail)
            await self.emit(
                "storage.media_dir_unavailable",
                {"media_dir": str(self.config.storage.media_dir), "error": str(exc)},
            )
        self.notifier.start()  # sync: spawns one consumer task per notification rule
        self.mqtt.start()  # sync too; no-op unless integrations.mqtt.enabled
        self.intake.start()  # incidents → event rows, state observed
        self.verdicts.start()  # closed incidents → confirmed / dismissed (VLM judge)
        if self.telegram is not None:
            self.telegram.start()  # alerts, digests, commands and buttons (ADR-0013)
        if self.frigate_source is not None:
            self.frigate_source.start()  # last: consumers above are subscribed already
        if self.clip_folders is not None:
            self.clip_folders.start()
        self._workers_started = True

    async def stop(self) -> None:
        stops: list[tuple[str, Callable[[], Awaitable[None]]]] = []
        if self.clip_folders is not None:
            stops.append(("clip folders", self.clip_folders.stop))
        if self.frigate_source is not None:
            stops.append(("frigate source", self.frigate_source.stop))
        if self.telegram is not None:
            stops.append(("telegram", self.telegram.stop))
        stops += [
            ("verdicts", self.verdicts.stop),
            ("intake", self.intake.stop),
            ("mqtt", self.mqtt.stop),
            ("notifier", self.notifier.stop),
        ]
        if self.frigate_client is not None:
            stops.append(("frigate client", self.frigate_client.aclose))
        stops += [(f"vlm client {n}", c.aclose) for n, c in enumerate(self._vlm_clients)]
        for name, stop in stops:
            try:
                await stop()
            except Exception:
                logger.exception("stopping %s failed", name)
        self._workers_started = False
        await self.db.close()
