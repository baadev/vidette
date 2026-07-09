"""Application runtime: owns and wires every subsystem.

Boot sequence (FastAPI lifespan calls start/stop):
1. Database.connect (migrations).
2. Go2rtcManager.sync() — config generation is unconditional; gateway reachability is not
   required to boot (health surfaces it).
3. With `workers=True`: ExportManager, RecorderSupervisor, Janitor, PreviewWorker,
   NotificationDispatcher, PipelineSupervisor + EventEngine (M2 cascade tiers 0–2).
   The Tier-1 detector loads (and possibly downloads its model) in the *background*:
   boot stays fast, the cascade runs motion-only until the detector is ready, and both
   the transition and any failure are loud system events. An unusable media dir or
   missing ffmpeg downgrades recording the same way instead of crashing boot — the API
   and wizard must stay reachable precisely when things are broken.
4. stop() reverses in order; every subsystem stop is awaited and exception-contained.

System events flow two ways on purpose: persisted via Database.add_system_event (the
audit trail the API serves) and published on the in-process bus (what the notification
dispatcher and future WebSocket feed consume). `emit()` is the single helper that does
both; M1 subsystems that only know the Database are bridged through its hook.

Environment (documented in docs/configuration.md):
  VIDETTE_CONFIG       config file path         (default /config/vidette.yaml)
  VIDETTE_GO2RTC_URL   gateway API              (default http://go2rtc:1984)
  VIDETTE_GO2RTC_RTSP  gateway RTSP restream    (default rtsp://go2rtc:8554)
  VIDETTE_GO2RTC_CONF  generated gateway config (default: next to the database)
  VIDETTE_WEB_DIST     built web app dir

A missing config file is not an error: it boots with VidetteConfig() defaults so the
first-run wizard has an API to talk to (wizard-mode).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from pathlib import Path
from typing import Any

from vidette.auth.service import AuthService
from vidette.core.config import VidetteConfig, load_config
from vidette.core.events import InProcessEventBus
from vidette.db import Database
from vidette.events.engine import EventEngine
from vidette.notify.dispatcher import NotificationDispatcher
from vidette.pipeline.base import Detection
from vidette.pipeline.detect import NullDetector, OnnxDetector
from vidette.pipeline.runner import PipelineSupervisor
from vidette.recording.exporter import ExportManager
from vidette.recording.janitor import Janitor
from vidette.recording.previews import PreviewWorker
from vidette.recording.recorder import RecorderSupervisor
from vidette.streams.go2rtc import DEFAULT_API_URL, DEFAULT_RTSP_BASE, Go2rtcManager

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

        gateway_conf_env = os.environ.get("VIDETTE_GO2RTC_CONF")
        gateway_conf = (
            Path(gateway_conf_env)
            if gateway_conf_env
            else config.storage.database.parent / "go2rtc.yaml"
        )
        self.go2rtc = Go2rtcManager(
            config,
            api_url=os.environ.get("VIDETTE_GO2RTC_URL", DEFAULT_API_URL),
            rtsp_base=os.environ.get("VIDETTE_GO2RTC_RTSP", DEFAULT_RTSP_BASE),
            config_path=gateway_conf,
        )
        self.recorder = RecorderSupervisor(
            config, self.db, self.go2rtc, media_dir=config.storage.media_dir
        )
        self.exporter = ExportManager(config, self.db, media_dir=config.storage.media_dir)
        self.janitor = Janitor(config, self.db, self.exporter)
        self.previews = PreviewWorker(config, self.db, media_dir=config.storage.media_dir)

        # --- M2 cascade -------------------------------------------------------------------
        self._detector: OnnxDetector | NullDetector = NullDetector()
        self.detector_state = "loading"  # loading | ready | disabled
        self._detect_semaphore = asyncio.Semaphore(1)  # one inference at a time (CPU budget)
        self.engine = EventEngine(
            config,
            self.db,
            self.bus,
            snapshot_fn=self.go2rtc.snapshot,
            media_dir=config.storage.media_dir,
        )
        self.pipeline = PipelineSupervisor(
            config,
            self.go2rtc,
            self._detect,
            self.engine.on_detections,
            self.emit,
        )
        self.notifier = NotificationDispatcher(
            config, self.bus, emit=self.emit, base_url=config.server.base_url
        )

        self._detector_task: asyncio.Task[None] | None = None
        self._engine_tick_task: asyncio.Task[None] | None = None
        self._workers_started = False

    @classmethod
    def from_environment(cls) -> AppRuntime:
        path = default_config_path()
        if path.exists():
            config, warnings = load_config(path)
        else:
            config = VidetteConfig()
            warnings = [
                f"config file {path} not found — starting in wizard mode with defaults; "
                "create it from deploy/config.example.yaml"
            ]
        return cls(config, config_warnings=warnings)

    # --- system events -------------------------------------------------------------------

    @staticmethod
    def _bus_topic(kind: str) -> str | None:
        """Map a system-event kind to its bus topic.

        Everything operational lives under `system.*` so the documented notification rule
        patterns match (`storage.pressure` → `system.storage.pressure`). Delivery-failure
        kinds (`notify.*`) are persisted but never published — a failing webhook matched by
        a `system.*` rule would otherwise notify about its own failure, forever.
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

    # --- detector ------------------------------------------------------------------------

    async def _detect(self, frame: Any) -> list[Detection]:
        async with self._detect_semaphore:
            return await self._detector.infer(frame)

    async def _load_detector(self) -> None:
        models_dir = self.config.storage.database.parent / "models"
        try:
            detector = await OnnxDetector.create(self.config.understanding.detector, models_dir)
        except Exception as exc:  # DetectorError or anything else: degrade, never crash boot
            self.detector_state = "disabled"
            await self.emit(
                "pipeline.detector_failed",
                {
                    "error": str(exc),
                    "action": "the cascade runs motion-only; check network access for the "
                    "model download and the models dir, then restart",
                },
            )
            return
        self._detector = detector
        self.detector_state = "ready"
        await self.emit(
            "pipeline.detector_ready",
            {"model": detector.spec.key, "provider": detector.provider},
        )

    # --- lifecycle -------------------------------------------------------------------------

    async def start(self, *, workers: bool = True) -> None:
        await self.db.connect()

        # Bridge M1 subsystems (recorder/janitor write straight to the DB): mirror their
        # system events onto the bus so notification rules on `system.*` see everything.
        async def _mirror(kind: str, payload: dict[str, Any]) -> None:
            topic = self._bus_topic(kind)
            if topic is not None:
                await self.bus.publish(topic, payload)

        self.db.system_event_hook = _mirror

        await self.go2rtc.sync()

        if not workers:
            return

        media_ok = True
        try:
            self.config.storage.media_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            media_ok = False
            detail = (
                f"media dir {self.config.storage.media_dir} is not writable: {exc} — "
                "recording, export, retention and analysis are disabled until it is fixed"
            )
            logger.error(detail)
            self.config_warnings.append(detail)
            await self.db.add_system_event(
                "storage.media_dir_unavailable",
                {"media_dir": str(self.config.storage.media_dir), "error": str(exc)},
            )

        self.notifier.start()  # sync: spawns one consumer task per notification rule
        if media_ok:
            await self.exporter.start()
            await self.recorder.start()  # handles missing ffmpeg with a loud system event
            await self.janitor.start()
            await self.previews.start()
            await self.pipeline.start()
            self._detector_task = asyncio.create_task(
                self._load_detector(), name="vidette-detector-load"
            )
            self._engine_tick_task = asyncio.create_task(
                self._engine_ticker(), name="vidette-engine-tick"
            )
            self._workers_started = True

    async def _engine_ticker(self) -> None:
        """Idle heartbeat: closes open events when a camera goes silent (no detections →
        `on_detections` stops firing, so someone else must call `tick`)."""
        while True:
            await asyncio.sleep(5)
            try:
                await self.engine.tick(time.time())
            except Exception:
                logger.exception("event engine tick failed")

    async def stop(self) -> None:
        for task_attr in ("_detector_task", "_engine_tick_task"):
            task: asyncio.Task[None] | None = getattr(self, task_attr)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                setattr(self, task_attr, None)
        if self._workers_started:
            for name, subsystem in (
                ("pipeline", self.pipeline),
                ("previews", self.previews),
                ("janitor", self.janitor),
                ("recorder", self.recorder),
                ("exporter", self.exporter),
            ):
                try:
                    await subsystem.stop()
                except Exception:
                    logger.exception("stopping %s failed", name)
            self._workers_started = False
        try:
            await self.notifier.stop()
        except Exception:
            logger.exception("stopping notifier failed")
        try:
            await self.go2rtc.close()
        except Exception:
            logger.exception("closing gateway client failed")
        await self.db.close()
