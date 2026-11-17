"""Application runtime: owns and wires every subsystem.

Boot sequence (FastAPI lifespan calls start/stop):
1. Database.connect (migrations).
2. With `workers=True`: NotificationDispatcher and MqttPublisher (both no-ops without
   configured rules / broker), the EventIntake (incidents → event rows), the verdict
   worker (with calibration from 👍/👎, ADR-0020), the Telegram bot when
   `notifications.telegram` is set (ADR-0013), then the
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

import asyncio
import contextlib
import dataclasses
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from vidette.auth.service import AuthService
from vidette.calibration.service import Calibrator
from vidette.clipfolder import ClipFolderSource
from vidette.core.config import VidetteConfig, VlmBackend, load_config
from vidette.core.events import InProcessEventBus
from vidette.db import Database
from vidette.deep.analyst import DeepAnalyst
from vidette.deep.worker import DeepWorker
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
from vidette.people.merge import merge_pass
from vidette.people.models import ModelError
from vidette.people.recurrence import with_recurrence
from vidette.people.setup import engine_for, identities_for, merge_settings_for, store_for
from vidette.people.store import PeopleStore, StoreError
from vidette.telegram import i18n
from vidette.telegram.i18n import t
from vidette.telegram.service import TelegramService
from vidette.verdicts.base import Judge
from vidette.verdicts.vlm import VlmJudge
from vidette.verdicts.worker import VerdictWorker
from vidette.vlm.budget import KeyedLedger, MetaLedger
from vidette.vlm.client import VlmClient
from vidette.vlm.endpoint import (
    BACKEND_META_KEY,
    VlmEndpoint,
    effective_backend,
    endpoint_for,
    parse_choice,
)
from vidette.vlm.gate import SlotGate

logger = logging.getLogger(__name__)


def default_config_path() -> Path:
    return Path(os.environ.get("VIDETTE_CONFIG", "/config/vidette.yaml"))


class AppRuntime:
    def __init__(self, config: VidetteConfig, *, config_warnings: list[str] | None = None) -> None:
        self.config = config
        self.config_warnings = list(config_warnings or [])
        if config.notifications.telegram is not None:  # the bot's language, process-wide
            i18n.use(config.notifications.telegram.language)
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
        # one model slot per server for everything in this process (ADR-0019)
        self._gates: dict[str, SlotGate] = {}
        self.people_store: PeopleStore | None = None
        self.face_engine: FaceEngine | None = None
        self.people_check: PeopleCheck | None = None
        self.people_error: str | None = None
        self.identities: IdentityStore | None = None  # people.track_unknown (ADR-0014)
        if config.people.enabled:
            self._setup_people()
        # 👍/👎 change who is woken (ADR-0020): steps stored in the database, never the config
        self.calibration = Calibrator(config, self.db, emit=self.emit)
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
            calibration=self.calibration,
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
                gate_for=self.gate_for,
                calibration=self.calibration,
            )
        self.deep: DeepWorker | None = None
        if config.understanding.deep.enabled:
            self.deep = self._make_deep()
        self._merge_task: asyncio.Task[None] | None = None
        self._workers_started = False

    async def judge_line(self) -> str:
        """The judge in one line for people (the bot's /status)."""
        backend = await self.vlm_backend()
        if backend is VlmBackend.none:
            return t("judge.off")
        endpoint = endpoint_for(self.config, backend)
        status = self.verdicts.status
        model = endpoint.model.rstrip("/").rsplit("/", 1)[-1]
        line = t(
            "judge.line", backend=backend.value, model=model, judged=status.judged,
            skipped=status.skipped_old,
        )  # fmt: skip
        if status.last_error:
            line += t("judge.error", error=status.last_error[:80])
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

    def gate_for(self, endpoint: VlmEndpoint) -> SlotGate:
        """The shared slot of one model server (vidette.vlm.gate)."""
        return self._gates.setdefault(endpoint.base_url, SlotGate())

    def _make_judge(self, backend: VlmBackend) -> Judge:
        endpoint = endpoint_for(self.config, backend)
        client = VlmClient(endpoint, ledger=self.vlm_ledger, gate=self.gate_for(endpoint))
        self._vlm_clients.append(client)
        vlm = self.config.understanding.vlm
        return VlmJudge(client=client, frames_per_call=vlm.frames_per_call, language=vlm.language)

    def _make_deep(self) -> DeepWorker:
        """Deep analysis (ADR-0019) on the judge's own endpoint, its calls counted apart."""
        deep = self.config.understanding.deep
        vlm = self.config.understanding.vlm
        endpoint = dataclasses.replace(
            endpoint_for(self.config, VlmBackend(deep.backend)),
            max_output_tokens=deep.max_output_tokens,
            # the clip cap, when set, is the real limit; the calls budget only stops a runaway
            max_calls_per_day=(deep.max_clips_per_day or 2000) * 8,
        )
        client = VlmClient(endpoint, ledger=KeyedLedger(self.vlm_ledger, "deep-"))
        self._vlm_clients.append(client)
        analyst = DeepAnalyst(
            client,
            frames=deep.frames,
            frames_per_call=deep.frames_per_call or min(deep.frames, 8),
            language=deep.language or vlm.language,
        )
        return DeepWorker(
            self.config,
            self.db,
            analyst,
            self.gate_for(endpoint),
            media_dir=self.config.storage.media_dir,
            emit=self.emit,
            endpoint=endpoint,
            live_busy=lambda: self.verdicts.working or self.verdicts.backlog > 0,
        )

    async def _identity_merges(self) -> None:
        """The merge pass (ADR-0018) after new sightings, at most every `merge.every`;
        a failure is logged and tried again next time, never stops anything else."""
        assert self.identities is not None
        store = self.identities
        settings = self.config.people.identities.merge
        seen = -1
        while True:
            try:
                newest = await asyncio.to_thread(store.last_sighting_id)
                if newest != seen:
                    plan, applied = await asyncio.to_thread(
                        merge_pass, store, merge_settings_for(self.config.people)
                    )
                    seen = newest
                    if applied.merged:
                        await self.emit(
                            "identities.merged",
                            {"run": applied.run, "merges": applied.merged,
                             "groups": len(plan.groups)},
                        )  # fmt: skip
            except Exception:
                logger.exception("identity merge pass failed")
            await asyncio.sleep(settings.every_s)

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
        if self.deep is not None:
            self.deep.start()  # deep analysis: the night window, one request at a time
        if self.identities is not None and self.config.people.identities.merge.auto:
            self._merge_task = asyncio.create_task(self._identity_merges(), name="merge")
        self._workers_started = True

    async def stop(self) -> None:
        stops: list[tuple[str, Callable[[], Awaitable[None]]]] = []
        merge, self._merge_task = self._merge_task, None
        if merge is not None:
            merge.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await merge
        if self.deep is not None:
            stops.append(("deep analysis", self.deep.stop))
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
