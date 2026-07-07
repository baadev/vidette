"""Application runtime: owns and wires every subsystem.

Boot sequence (FastAPI lifespan calls start/stop):
1. Database.connect (migrations).
2. Go2rtcManager.sync() — config generation is unconditional; gateway reachability is not
   required to boot (health surfaces it).
3. With `workers=True`: ExportManager, RecorderSupervisor, Janitor. An unusable media dir
   or missing ffmpeg downgrades recording with a loud system event instead of crashing
   boot — the API and wizard must stay reachable precisely when things are broken.
4. stop() reverses in order; every subsystem stop is awaited and exception-contained.

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

import logging
import os
from pathlib import Path

from vidette.auth.service import AuthService
from vidette.core.config import VidetteConfig, load_config
from vidette.db import Database
from vidette.recording.exporter import ExportManager
from vidette.recording.janitor import Janitor
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

    async def start(self, *, workers: bool = True) -> None:
        await self.db.connect()
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
                "recording, export and retention are disabled until it is fixed"
            )
            logger.error(detail)
            self.config_warnings.append(detail)
            await self.db.add_system_event(
                "storage.media_dir_unavailable",
                {"media_dir": str(self.config.storage.media_dir), "error": str(exc)},
            )

        if media_ok:
            await self.exporter.start()
            await self.recorder.start()  # handles missing ffmpeg with a loud system event
            await self.janitor.start()
            self._workers_started = True

    async def stop(self) -> None:
        if self._workers_started:
            for name, subsystem in (
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
            await self.go2rtc.close()
        except Exception:
            logger.exception("closing gateway client failed")
        await self.db.close()
