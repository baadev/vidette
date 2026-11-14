"""The verdict worker and the backend switch: closed incidents become confirmed or
dismissed events, only with a selected backend, only once, never from stale incidents."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fakes.openai_api import FakeVlmApi
from fastapi.testclient import TestClient
from test_clipfolder import collected_clip

from vidette.api.app import create_app
from vidette.core.config import VidetteConfig, VlmBackend
from vidette.core.events import InProcessEventBus
from vidette.core.incidents import Incident
from vidette.db import Database
from vidette.events.intake import EventIntake
from vidette.runtime import AppRuntime
from vidette.verdicts.base import Case, PolicyText, Verdict
from vidette.verdicts.worker import VerdictWorker


@dataclass
class ScriptedJudge:
    verdict: Verdict
    seen: list[tuple[Case, PolicyText]] = field(default_factory=list)
    name: str = "vlm-local"
    description: str = "scripted"

    async def judge(self, case: Case, policy: PolicyText) -> Verdict:
        self.seen.append((case, policy))
        return self.verdict


async def _emit(kind: str, payload: dict[str, Any]) -> None:
    return None


def config(tmp_path: Path, **extra: Any) -> VidetteConfig:
    data: dict[str, Any] = {
        "storage": {"media_dir": str(tmp_path / "media"), "database": str(tmp_path / "v.db")},
        "policies": [
            {"name": "entry", "description": "someone tries the door", "cameras": ["porch"]},
        ],
    }
    data.update(extra)
    return VidetteConfig.model_validate(data)


async def setup(
    tmp_path: Path, verdict: Verdict, *, backend: VlmBackend = VlmBackend.local, **extra: Any
) -> tuple[VerdictWorker, ScriptedJudge, Database, InProcessEventBus, EventIntake]:
    cfg = config(tmp_path, **extra)
    db = Database(cfg.storage.database)
    await db.connect()
    bus = InProcessEventBus()
    intake = EventIntake(db, bus, media_dir=cfg.storage.media_dir)
    judge = ScriptedJudge(verdict)

    async def resolver() -> VlmBackend:
        return backend

    worker = VerdictWorker(
        cfg, bus, db, emit=_emit, backend=resolver, judges=lambda _b: judge,
        media_dir=cfg.storage.media_dir,
    )  # fmt: skip
    return worker, judge, db, bus, intake


def incident(camera: str = "porch", ended_ago: float = 5.0) -> Incident:
    now = time.time()
    return Incident(
        source="clip-folder", source_id=f"clips/{camera}-{now}.mp4", camera=camera,
        started_at=now - ended_ago - 20, ended_at=now - ended_ago,
    )  # fmt: skip


async def observed(intake: EventIntake, item: Incident) -> None:
    await intake.handle("incident.closed", item.model_dump(mode="json"))


ALERT = Verdict(alert=True, confidence=0.9, rationale="A person pulls the handle.",
                label="door_handle")  # fmt: skip
SILENT = Verdict(alert=False, confidence=0.8, rationale="A courier leaves a parcel.")
UNSURE = Verdict(alert=False, abstained=True, rationale="timeout: no answer within 45 s")


async def test_alert_confirms_and_publishes(tmp_path: Path) -> None:
    worker, judge, db, bus, intake = await setup(tmp_path, ALERT)
    confirmed = bus.subscribe("event.confirmed")
    item = incident()
    await observed(intake, item)
    row = await worker.handle(item)
    assert row is not None and row.state == "confirmed" and row.policy == "entry"
    assert row.summary == "A person pulls the handle."
    assert row.intent is not None and row.intent["verdicts"][0]["label"] == "door_handle"
    topic, payload = await asyncio.wait_for(confirmed.get(), 1)
    assert topic == "event.confirmed" and payload["id"] == row.id
    assert await worker.handle(item) is None  # judged once: no longer observed
    assert len(judge.seen) == 1
    await db.close()


@pytest.mark.parametrize(
    ("verdict", "state", "summary"),
    [
        (SILENT, "dismissed", "A courier leaves a parcel."),
        (UNSURE, "observed", "no verdict: timeout: no answer within 45 s"),
    ],
)
async def test_silent_dismisses_and_no_verdict_stays_observed(
    tmp_path: Path, verdict: Verdict, state: str, summary: str
) -> None:
    worker, _judge, db, bus, intake = await setup(tmp_path, verdict)
    confirmed = bus.subscribe("event.confirmed")
    item = incident()
    await observed(intake, item)
    row = await worker.handle(item)
    assert row is not None and row.state == state and row.summary == summary
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(confirmed.get(), 0.1)  # nothing notifies
    await db.close()


async def test_sensitivity_sets_how_sure_an_alert_must_be(tmp_path: Path) -> None:
    unsure_alert = Verdict(alert=True, confidence=0.6, rationale="Maybe at the door.")
    policies = [{"name": "entry", "description": "x", "cameras": ["porch"],
                 "sensitivity": "relaxed"}]  # fmt: skip
    worker, _j, db, _bus, intake = await setup(tmp_path, unsure_alert, policies=policies)
    item = incident()
    await observed(intake, item)
    row = await worker.handle(item)
    assert row is not None and row.state == "dismissed"  # 0.6 < relaxed 0.75
    assert row.intent is not None and row.intent["verdicts"][0]["model_said_alert"] is True
    await db.close()


@pytest.mark.parametrize(
    ("backend", "item"),
    [
        (VlmBackend.none, incident()),  # off: incidents stay observed
        (VlmBackend.local, incident(camera="garden")),  # no policy for this camera
        (VlmBackend.local, incident(ended_ago=3600)),  # stale: never a burst of old alerts
    ],
)
async def test_nothing_is_judged_when_it_should_not_be(
    tmp_path: Path, backend: VlmBackend, item: Incident
) -> None:
    worker, judge, db, _bus, intake = await setup(tmp_path, ALERT, backend=backend)
    await observed(intake, item)
    assert await worker.handle(item) is None
    assert judge.seen == []
    row = await db.get_event_by_source(item.source, item.source_id)
    assert row is not None and row.state == "observed"
    await db.close()


# --- end to end: clip folder → event → VLM over HTTP → confirmed, and the switch ------


@pytest.fixture
def fake_vlm() -> Iterator[tuple[FakeVlmApi, str]]:
    api = FakeVlmApi(
        answer=lambda text, images, body: {
            "decision": "alert", "label": "door_handle", "confidence": 0.93,
            "description": "A person pulls the front door handle.",
        }
    )  # fmt: skip
    with api.serve() as url:
        yield api, url


def test_the_switch_turns_judging_on_and_a_clip_becomes_a_confirmed_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_vlm: tuple[FakeVlmApi, str]
) -> None:
    api, url = fake_vlm
    monkeypatch.setenv("VIDETTE_WEB_DIST", str(tmp_path / "no-web-dist"))
    dataset = tmp_path / "front-door"
    dataset.mkdir()
    cfg = VidetteConfig.model_validate(
        {
            "server": {"auth": {"mode": "none"}},
            "storage": {"media_dir": str(tmp_path / "media"), "database": str(tmp_path / "v.db")},
            "clip_folders": [{"path": str(dataset), "poll_every": "1s", "settle": "0s"}],
            "understanding": {"vlm": {"backend": "none", "local": {"base_url": url,
                                                                   "model": "fake-vlm"}}},
            "policies": [{"name": "entry-interest", "description": "door attempts"}],
        }
    )  # fmt: skip
    runtime = AppRuntime(cfg)
    confirmed = runtime.bus.subscribe("event.confirmed")
    with TestClient(create_app(runtime, workers=True)) as client:
        settings = client.get("/api/v1/settings/vlm").json()
        assert settings["backend"] == "none" and settings["source"] == "config"
        options = {o["id"]: o for o in settings["options"]}
        assert options["local"]["privacy"].startswith("Frames stay on this machine")
        assert options["cloud"]["available"] is False and "cloud" in options["cloud"]["reason"]
        assert client.put("/api/v1/settings/vlm", json={"backend": "cloud"}).status_code == 409

        switched = client.put("/api/v1/settings/vlm", json={"backend": "local"}).json()
        assert switched["backend"] == "local" and switched["source"] == "ui"
        kinds = [e["kind"] for e in client.get("/api/v1/system/events").json()]
        assert "vlm.backend_changed" in kinds

        client.portal.call(asyncio.sleep, 0.3)  # the first scan sets the folder watermark
        # The worker judges only incidents that ended in the last 15 minutes: a recent clip.
        now = datetime.now().astimezone().replace(microsecond=0)
        collected_clip(dataset, f"{now:%Y%m%d-%H%M%S}", camera="porch",
                       started=now - timedelta(seconds=10))  # fmt: skip
        for _ in range(100):
            events = client.get("/api/v1/events").json()
            if events and events[0]["state"] == "confirmed":
                break
            client.portal.call(asyncio.sleep, 0.05)
        event = events[0]
        assert event["state"] == "confirmed" and event["policy"] == "entry-interest"
        assert event["summary"] == "A person pulls the front door handle."
        topic, _payload = client.portal.call(asyncio.wait_for, confirmed.get(), 2)
        assert topic == "event.confirmed"
        usage = client.get("/api/v1/settings/vlm").json()
        local = next(o for o in usage["options"] if o["id"] == "local")
        assert local["usage_today"]["calls"] == 1 and usage["worker"]["confirmed"] == 1
    body = api.requests[0]["body"]
    assert len([p for p in body["messages"][1]["content"] if p["type"] == "image_url"]) == 4
